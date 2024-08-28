import dataclasses
import time
import dgl
import torch
import argparse
import subprocess
from ogb.nodeproppred import DglNodePropPredDataset
from torch.nn.parallel import DistributedDataParallel as DDP
from numa import numa_info
from typing import List

@dataclasses.dataclass
class LogStep:
    epoch: int
    eval_acc: float
    sample_time: float
    load_time: float
    forward_time: float
    backward_time: float
    cur_epoch_time: float  # exclude evaluate time
    acc_epoch_time: float  # accumulative epoch time excluding evaluate time
    evaluate_time: float
    loss: float

    def print(self):
        print(
            "Epoch {:05d} | Loss {:.4f} | Accuracy {:.4f} | Epoch Time {:.4f}".format(
                self.epoch, self.loss, self.eval_acc, self.cur_epoch_time
            ),
            flush=True,
        )

    def dict(self):
        return self.__dict__

@dataclasses.dataclass
class Logger:
    steps: List[LogStep] = None

    def __init__(self):
        self.steps = []

    def append(self, step: LogStep):
        self.steps.append(step)

    def list(self):
        ret = []
        for step in self.steps:
            ret.append(step.dict())

        return ret

class Timer:
    def __init__(self):
        self.start_time = None
        self.last_record = None
        self.end_time = None

    def start(self):
        if self.start_time is not None:
            raise RuntimeError(
                "Timer is already running. Use stop() to stop it before starting again."
            )
        self.start_time = time.time()
        self.last_record = time.time()

    def stop(self):
        if self.start_time is None:
            raise RuntimeError("Timer is not running. Use start() to start it.")
        self.end_time = time.time()
        elapsed_time = self.end_time - self.start_time
        self.start_time = None  # Reset the timer
        self.end_time = None
        self.last_record = None
        return elapsed_time

    def record(self):
        if self.last_record is None:
            raise RuntimeError("Timer is not running. Use start() to start it.")
        current = time.time()
        elapsed_time = current - self.last_record
        self.last_record = time.time()  # Reset the timer
        return elapsed_time


gconfig = None

@dataclasses.dataclass
class Config:
    sample_mode: str
    batch_size: int  # Minibatch only
    fanouts: List[int]  # Minibatch only
    num_epoch: int
    hid_size: int
    num_head: int
    lr: float
    weight_decay: float
    dropout: float
    world_size: int
    num_partition: int
    graph_name: str
    data_dir: str
    model: str
    log_file: str
    eval: bool

    def __init__(self, args):
        self.sample_mode = args.sample_mode
        self.batch_size = args.batch_size
        self.fanouts = args.fanouts
        self.num_epoch = args.num_epoch
        self.hid_size = args.hid_size
        self.num_layers = args.num_layers
        self.num_head = args.num_head
        self.lr = args.lr
        self.weight_decay = args.weight_decay
        # self.world_size = args.world_size
        self.dropout = args.dropout
        self.num_partition = args.num_partition
        self.graph_name = args.graph_name
        self.data_dir = args.data_dir
        self.model = args.model
        self.log_file = args.log_file
        self.eval = args.eval

    @staticmethod
    def get_global_config():
        global gconfig
        return gconfig
    
    @staticmethod
    def set_global_config(config):
        global gconfig
        gconfig = config
        
@dataclasses.dataclass
class Dataset:
    graph: dgl.DGLGraph
    feat: torch.Tensor
    label: torch.Tensor
    train_mask: torch.Tensor
    val_mask: torch.Tensor
    test_mask: torch.Tensor
    num_classes: int
    in_feats: int

    def __init__(
        self, graph, feat, label, train_mask, val_mask, test_mask, num_classes, in_feats
    ):
        self.graph = graph
        self.feat = feat
        self.label = label
        self.train_mask = train_mask
        self.val_mask = val_mask
        self.test_mask = test_mask
        self.num_classes = num_classes
        self.in_feats = in_feats

    def to(self, device):
        self.label = self.label.to(device)
        self.train_mask = self.train_mask.to(device)
        self.val_mask = self.val_mask.to(device)
        self.test_mask = self.test_mask.to(device)


def get_num_numa():
    return len(numa_info.keys())

def get_load_compute_cores(numa_id: int = 0):
    all_threads = numa_info[numa_id]
    num_cores = len(all_threads) // 2
    loader_cores = all_threads[:num_cores]
    compute_cores = all_threads[num_cores:]
    return loader_cores, compute_cores

def str_to_bytes(cache_size: str):
    if "G" in cache_size:
        n, _ = cache_size.split('G')
        return float(n) * 1024 * 1024 * 1024
    elif 'M' in cache_size:
        n, _ = cache_size.split('M')
        return float(n) * 1024 * 1024
    elif 'K' in cache_size:
        n, _ = cache_size.split('K')
        return float(n) * 1024

def tensor_to_bytes(t: torch.Tensor):
    sz = t.nelement() * t.element_size()
    return sz
    
def get_cuda_gpu_model():
    try:
        # Execute the nvidia-smi command
        result = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], 
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Check if there was an error
        if result.returncode != 0:
            print("Error executing nvidia-smi:", result.stderr)
            return []
        
        # Parse the output into a list of GPU model
        gpu_model = result.stdout.strip().split('\n')
        
        return gpu_model

    except FileNotFoundError:
        print("nvidia-smi not found. Make sure NVIDIA drivers are installed and accessible.")
        return []
        
def get_cpu_model() -> str:
    ret = subprocess.check_output("lscpu", shell=True).strip().decode()
    idx = ret.find("Model name:")
    cpu_model = ret[idx+11:].strip().split("\n")[0]
    return cpu_model

def get_train_meta(config: Config):
    ret = dict()
    ret["weight_decay"] = config.weight_decay
    ret["learning_rate"] = config.lr
    ret["dropout"] = config.dropout
    return ret

def get_full_meta(config: Config, data: Dataset):
    ret = dict()
    ret["graph_name"] = config.graph_name
    ret["train_mode"] = "full"
    ret["cpu_model"] = get_cpu_model()
    ret["num_node"] = data.graph.num_nodes()
    ret["num_edge"] = data.graph.num_edges()
    ret["feat_width"] = data.in_feats
    ret["num_epoch"] = config.num_epoch
    ret["num_partition"] = config.num_partition
    return ret

def get_minibatch_meta(config: Config, data: Dataset):
    ret = dict()
    ret["graph_name"] = config.graph_name
    ret["train_mode"] = "minibatch"
    ret["num_node"] = data.graph.num_nodes()
    ret["num_edge"] = data.graph.num_edges()
    ret["cpu_model"] = get_cpu_model()
    ret["feat_width"] = data.in_feats
    ret["batch_size"] = config.batch_size
    ret["fanouts"] = config.fanouts
    ret["num_epoch"] = config.num_epoch
    ret["num_partition"] = config.num_partition
    return ret

def get_quiver_meta(config: Config, data: Dataset):
    ret = dict()
    ret["graph_name"] = config.graph_name
    ret["system_name"] = "quiver"
    ret["train_mode"] = "minibatch"
    ret["sample_mode"] = config.sample_mode
    ret["num_node"] = data.graph.num_nodes()
    ret["num_edge"] = data.graph.num_edges()
    ret["cpu_model"] = get_cpu_model()
    ret["gpu_model"] = get_cuda_gpu_model()
    ret["feat_width"] = data.in_feats
    ret["batch_size"] = config.batch_size
    ret["fanouts"] = config.fanouts
    ret["num_epoch"] = config.num_epoch
    ret["num_partition"] = config.num_partition
    return ret

def get_args() -> Config:
    parser = argparse.ArgumentParser(description="local run script")
    parser.add_argument(
        "--sample_mode", default="gpu", type=str, help="Sample device (default: gpu)", choices=["gpu", "uva", "cpu"]
    )
    
    parser.add_argument(
        "--batch_size", default=1024, type=int, help="Global batch size (default: 1024)"
    )
    
    parser.add_argument(
        "--fanouts",
        default="15,15,15",
        type=lambda fanouts: [int(fanout) for fanout in fanouts.split(",")],
        help="Fanouts",
    )
    
    parser.add_argument(
        "--num_epoch", default=1, type=int, help="Number of epochs to train (default 1)"
    )
    parser.add_argument(
        "--hid_size", default=256, type=int, help="Model hidden dimension"
    )
    parser.add_argument("--num_layers", default=3, type=int, help="Model layers")
    parser.add_argument(
        "--num_head", default=4, type=int, help="GAT only: number of attention head"
    )
    
    parser.add_argument(
        "--lr", default=5e-3, type=float, help="learning rate"
    )
    
    parser.add_argument(
        "--weight_decay", default=5e-4, type=float, help="weight decay"
    )
    
    parser.add_argument(
        "--dropout", default=0.5, type=float, help="dropout ratio"
    )
    
    parser.add_argument("--world_size", default=1, type=int, help="Number of Hosts")
    parser.add_argument(
        "--num_partition", default=1, type=int, help="Number of partitions"
    )
    
    parser.add_argument(
        "--graph_name",
        default="ogbn-arxiv",
        type=str,
        help="Input graph name",
        choices=[
            "ogbn-proteins",
            "pubmed",
            "reddit",
            "ogbn-products",
            "ogbn-arxiv",
            "ogbn-mag",
            "ogbn-papers100M",
        ],
    )
    
    parser.add_argument('--data_dir', required=True, type=str, help="Root data directory")
    # parser.add_argument(
    #     "--data_dir",
    #     default="/data/juelin/dataset/gnn",
    #     type=str,
    #     help="Root data directory",
    # )
    
    parser.add_argument(
        "--model",
        default="gat",
        type=str,
        help="Model type",
        choices=["gcn", "gat", "sage"],
    )
    parser.add_argument(
        "--log_file", default="log.json", type=str, help="output log file"
    )

    parser.add_argument("--eval", default=True, action=argparse.BooleanOptionalAction)

    args = parser.parse_args()
    config = Config(args)
    Config.set_global_config(config)
    return config


def load_dataset(config: Config, topo_only=False):
    if "ogbn" in config.graph_name:
        dataset = DglNodePropPredDataset(name=config.graph_name, root=config.data_dir)
        g, label = dataset[0]
        g = dgl.add_self_loop(g)
        label = torch.flatten(label).to(torch.int64)
        feat = g.ndata.pop("feat")
        idx_split = dataset.get_idx_split()
        train_mask = idx_split["train"]
        val_mask = idx_split["valid"]
        test_mask = idx_split["test"]
        in_feats = feat.shape[1]
        num_classes = dataset.num_classes

        if topo_only:
            feat = None

        return Dataset(
            graph=g,
            feat=feat,
            label=label,
            train_mask=train_mask,
            val_mask=val_mask,
            test_mask=test_mask,
            num_classes=num_classes,
            in_feats=in_feats,
        )

    elif config.graph_name in ["pubmed", "reddit"]:
        dataset = None
        if config.graph_name == "pubmed":
            dataset = dgl.data.PubmedGraphDataset(
                raw_dir=config.data_dir, transform=dgl.add_self_loop
            )
        elif config.graph_name == "reddit":
            dataset = dgl.data.RedditDataset(
                raw_dir=config.data_dir, transform=dgl.add_self_loop
            )

        g: dgl.DGLGraph = dataset[0]
        indices = torch.arange(g.num_nodes())
        label = g.ndata.pop("label")
        feat = g.ndata.pop("feat")
        train_mask = indices[g.ndata.pop("train_mask")]
        val_mask = indices[g.ndata.pop("val_mask")]
        test_mask = indices[g.ndata.pop("test_mask")]

        label = torch.flatten(label).to(torch.int64)
        in_feats = feat.shape[1]
        num_classes = dataset.num_classes

        if topo_only:
            feat = None

        return Dataset(
            graph=g,
            feat=feat,
            label=label,
            train_mask=train_mask,
            val_mask=val_mask,
            test_mask=test_mask,
            num_classes=num_classes,
            in_feats=in_feats,
        )