import ast
import json
import logging
import math
import os
import random
import h5py
from dataclasses import dataclass

import braceexpand
import numpy as np
import pandas as pd
import torch
import torchvision.datasets as datasets
import webdataset as wds
from PIL import Image
from torch.utils.data import Dataset, DataLoader, SubsetRandomSampler
from torch.utils.data.distributed import DistributedSampler
from functools import partial
import soundfile as sf
import io
import torchaudio.functional as F
from pathlib import Path
import wget
from open_clip import tokenize
from open_clip.utils import dataset_split

try:
    import horovod.torch as hvd
except ImportError:
    hvd = None


from open_clip import tokenize

# initizlied the audioset map
_AUDIOSET_MAP_PATH = os.path.join(Path(__file__).parent, "audioset_textmap.npy")
_AUDIOSET_MAP = np.load(_AUDIOSET_MAP_PATH, allow_pickle=True)


def int16_to_float32(x):
    return (x / 32767.0).astype(np.float32)


# For Toy Dataset
class ToyDataset(Dataset):
    def __init__(self, index_path, ipc, config, eval_mode=False):
        """Toy Dataset for testing the audioset input with text labels

        Parameters
        ----------
            index_path: str
                the link to the h5 file of each audio
            idc: str
                the link to the npy file, the number of samples in each class
            config: dict
                the audio cfg file
           eval_model (bool): to indicate if the dataset is a testing dataset
        """
        self.audio_cfg = config["audio_cfg"]
        self.text_cfg = config["text_cfg"]
        self.fp = h5py.File(index_path, "r")
        self.ipc = np.load(ipc, allow_pickle=True)
        self.total_size = len(self.fp["audio_name"])
        self.classes_num = self.audio_cfg["class_num"]
        self.eval_mode = eval_mode

        if not eval_mode:
            self.generate_queue()
        else:
            self.queue = []
            for i in range(self.total_size):
                target = self.fp["target"][i]
                if np.sum(target) > 0:
                    self.queue.append(i)
            self.total_size = len(self.queue)
        logging.info("total dataset size: %d" % (self.total_size))
        logging.info("class num: %d" % (self.classes_num))

    def time_shifting(self, x):
        frame_num = len(x)
        shift_len = random.randint(0, frame_num - 1)
        new_sample = np.concatenate([x[shift_len:], x[:shift_len]], axis=0)
        return new_sample

    def generate_queue(self):
        self.queue = []
        while len(self.queue) < self.total_size:
            class_set = [*range(self.classes_num)]
            random.shuffle(class_set)
            self.queue += [
                self.ipc[d][random.randint(0, len(self.ipc[d]) - 1)] for d in class_set
            ]
        self.queue = self.queue[: self.total_size]

        logging.info("queue regenerated:%s" % (self.queue[-5:]))

    def crop_wav(self, x):
        crop_size = self.audio_cfg["crop_size"]
        crop_pos = random.randint(0, len(x) - crop_size - 1)
        return x[crop_pos : crop_pos + crop_size]

    def prompt_text(self, target):
        events = _AUDIOSET_MAP[np.where(target > 0)]
        event_text = "The sounds of " + ", ".join(events[:-1]) + " and " + events[-1]
        text = tokenize(event_text)[0]
        return text

    def __getitem__(self, index):
        """Load waveform, text, and target of an audio clip

        Parameters
        ----------
            index: int
                the index number
        Return
        ------
            output: dict {
                "hdf5_path": str,
                "index_in_hdf5": int,
                "audio_name": str,
                "waveform": list (audio_length,),
                "target": list (class_num, ),
                "text": torch.tensor (context_length,)
            }
                the output dictionary
        """
        s_index = self.queue[index]

        audio_name = self.fp["audio_name"][s_index].decode()
        # Hardcode here CHANGE
        hdf5_path = (
            self.fp["hdf5_path"][s_index]
            .decode()
            .replace(
                "/home/tiger/DB/knut/data/audioset/hdf5s/waveforms",
                "/mnt/audio_clip/test/data",
            )
        )
        r_idx = self.fp["index_in_hdf5"][s_index]
        target = self.fp["target"][s_index].astype(np.float32)
        text = self.prompt_text(target)
        with h5py.File(hdf5_path, "r") as f:
            waveform = int16_to_float32(f["waveform"][r_idx])[
                : self.audio_cfg["clip_samples"]
            ]
        assert (
            len(waveform) == self.audio_cfg["clip_samples"]
        ), "The sample length is not match"
        # Time shift
        # if (self.config.enable_time_shift) and (not self.eval_mode):
        #     waveform = self.time_shifting(waveform)
        # # Label Enhance
        # if (self.config.crop_size is not None) and (not self.eval_mode):
        #     waveform = self.crop_wav(waveform)
        # # the label enhance rate is fixed 0.5
        # if (self.config.enable_label_enhance) and (not self.eval_mode) and random.random() < 0.5:
        #     kidx = np.where(target)[0]
        #     for k in kidx:
        #         for add_key in self.class_map[k][1]:
        #             target[add_key] = 1.0
        #         if len(self.class_map[k][2]) > 0:
        #             add_key = random.choice(self.class_map[k][2])
        #             target[add_key] = 1.0

        # missing the text input

        data_dict = {
            "hdf5_path": hdf5_path,
            "index_in_hdf5": r_idx,
            "audio_name": audio_name,
            "waveform": waveform,
            "target": target,
            "text": text,
        }
        return data_dict

    def __len__(self):
        return self.total_size


class CsvDataset(Dataset):
    def __init__(self, input_filename, transforms, img_key, caption_key, sep="\t"):
        logging.debug(f"Loading csv data from {input_filename}.")
        df = pd.read_csv(input_filename, sep=sep)

        self.images = df[img_key].tolist()
        self.captions = df[caption_key].tolist()
        self.transforms = transforms
        logging.debug("Done loading data.")

    def __len__(self):
        return len(self.captions)

    def __getitem__(self, idx):
        images = self.transforms(Image.open(str(self.images[idx])))
        texts = tokenize([str(self.captions[idx])])[0]
        return images, texts


@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler


def preprocess_txt(text):
    return tokenize([str(text)])[0]


def get_dataset_size(shards, sizefilepath_=None, is_local=True):
    if isinstance(shards, list):
        size_list = []
        for s in shards:
            size_list.append(
                get_dataset_size(s, sizefilepath_=sizefilepath_, is_local=is_local)[0]
            )
    else:
        if not is_local:
            for n in dataset_split.keys():
                if n in shards.split("/"):
                    break
            for s in dataset_split[n]:
                if s in shards.split("/"):
                    break
            sizefilepath_ = f"./json_files/{n}/{s}/sizes.json"
        shards_list = list(braceexpand.braceexpand(shards))
        dir_path = os.path.dirname(shards)
        if sizefilepath_ is not None:
            sizes = json.load(open(sizefilepath_, "r"))
            total_size = sum(
                [
                    int(sizes[os.path.basename(shard.replace(".tar -", ".tar"))])
                    for shard in shards_list
                ]
            )
        else:
            sizes_filename = os.path.join(dir_path, "sizes.json")
            len_filename = os.path.join(dir_path, "__len__")
            if os.path.exists(sizes_filename):
                sizes = json.load(open(sizes_filename, "r"))
                total_size = sum(
                    [int(sizes[os.path.basename(shard)]) for shard in shards_list]
                )
            elif os.path.exists(len_filename):
                # FIXME this used to be eval(open(...)) but that seemed rather unsafe
                total_size = ast.literal_eval(open(len_filename, "r").read())
            else:
                raise Exception(
                    "Cannot find sizes file for dataset. Please specify the path to the file."
                )
                # total_size = None  # num samples undefined
                # some common dataset sizes (at time of authors last download)
                # cc3m-train: 2905954
                # cc12m: 10968539
                # LAION-400m: 407332084
        num_shards = len(shards_list)
    if isinstance(shards, list):
        return sum(size_list), len(shards)
    else:
        return total_size, num_shards


def get_imagenet(args, preprocess_fns, split):
    assert split in ["train", "val", "v2"]
    is_train = split == "train"
    preprocess_train, preprocess_val = preprocess_fns

    if split == "v2":
        from imagenetv2_pytorch import ImageNetV2Dataset

        dataset = ImageNetV2Dataset(location=args.imagenet_v2, transform=preprocess_val)
    else:
        if is_train:
            data_path = args.imagenet_train
            preprocess_fn = preprocess_train
        else:
            data_path = args.imagenet_val
            preprocess_fn = preprocess_val
        assert data_path

        dataset = datasets.ImageFolder(data_path, transform=preprocess_fn)

    if is_train:
        idxs = np.zeros(len(dataset.targets))
        target_array = np.array(dataset.targets)
        k = 50
        for c in range(1000):
            m = target_array == c
            n = len(idxs[m])
            arr = np.zeros(n)
            arr[:k] = 1
            np.random.shuffle(arr)
            idxs[m] = arr

        idxs = idxs.astype("int")
        sampler = SubsetRandomSampler(np.where(idxs)[0])
    else:
        sampler = None

    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        num_workers=args.workers,
        sampler=sampler,
    )

    return DataInfo(dataloader, sampler)


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def filter_no_caption(sample):
    return "txt" in sample


def log_and_continue(exn):
    """Call in an exception handler to ignore any exception, isssue a warning, and continue."""
    logging.warning(f"Handling webdataset error ({repr(exn)}). Ignoring.")
    return True


_SHARD_SHUFFLE_SIZE = 2000
_SHARD_SHUFFLE_INITIAL = 500
_SAMPLE_SHUFFLE_SIZE = 5000
_SAMPLE_SHUFFLE_INITIAL = 1000


def sample_prop(sizefile, inputs, proportion, is_local=True):
    """
    Sample a proportion of the data.
    """
    file_path_dict = {
        os.path.split(inputs[i])[1]: os.path.split(inputs[i])[0]
        for i in range(len(inputs))
    }
    sampled_filepath_dict = {}
    sampled_size_dict = {}
    if not is_local:
        if os.path.exists("sizes.json"):
            os.remove("sizes.json")
        wget.download(sizefile, "sizes.json")
        sizefile = "sizes.json"
    with open(sizefile, "r", encoding="UTF-8") as f:
        load_dict = json.load(f)
    L = int(len(file_path_dict) * proportion)
    subkeys = random.sample(file_path_dict.keys(), L)
    for k in subkeys:
        sampled_size_dict[k] = load_dict[k]
        sampled_filepath_dict[k] = file_path_dict[k]
    return (
        sum(sampled_size_dict.values()),
        L,
        [os.path.join(v, k) for k, v in sampled_filepath_dict.items()],
        sampled_size_dict,
    )


def preprocess(
    sample,
    audio_ext,
    text_ext,
    samplerate,
    mono,
    max_len,
    dtype,
    res_type,
    resample_method="TorchAudio",
):
    """
    Preprocess a single sample for wdsdataloader.
    """
    audio_data, orig_sr = sf.read(io.BytesIO(sample[audio_ext]))

    if len(audio_data) > max_len:  # random clip if too long
        overflow = len(audio_data) - max_len
        idx = np.random.randint(0, overflow + 1)
        audio_data = audio_data[idx : idx + max_len]
        # if np.random.rand() > 0.5:
        #    audio_data = audio_data[idx : idx + max_len]
        # else:
        #    audio_data = audio_data[
        #        len(audio_data) + 1 - idx - max_len : len(audio_data) + 1 - idx
        #    ]
    else:  # padding if too short
        audio_data = np.pad(
            audio_data,
            (0, max_len - len(audio_data)),
            mode="constant",
            constant_values=0,
        )

    # TODO: (yusong) add a key of "original audio"
    # TODO: (yusong) also return "original data"

    sample["waveform"] = torch.tensor(audio_data).float()
    del sample[audio_ext]
    texts = json.loads(sample[text_ext].decode("utf-8"))["text"]
    if isinstance(texts, list) and isinstance(texts[0], str) and len(texts) > 1:
        texts = random.choice(texts)
    sample["raw_text"] = texts
    sample["text"] = tokenize(texts)
    del sample[text_ext]
    sample["audio_name"] = sample["__key__"].split("/")[-1] + "." + audio_ext
    sample["text_name"] = sample["__key__"].split("/")[-1] + "." + text_ext
    sample["audio_orig_sr"] = orig_sr
    return sample


# TODO: Yusong: clear unused arguments
def get_wds_dataset(
    args,
    model_cfg,
    is_train,
    audio_ext="flac",
    text_ext="json",
    samplerate=48000,
    mono=True,
    max_len=480000,
    dtype="float64",
    res_type="kaiser_best",
    proportion=1.0,
    sizefilepath_=None,
    is_local=None,
    resample_method=None,
):
    """
    Get a dataset for wdsdataloader.
    """
    if is_local is None and (not args.remotedata is None):
        is_local = not args.remotedata

    input_shards = args.train_data if is_train else args.val_data
    assert input_shards is not None

    if not sizefilepath_ is None:
        sizefilepath = sizefilepath_
    else:
        sizefilepath = os.path.join(os.path.dirname(input_shards[0]), "sizes.json")

    if proportion != 1.0:
        num_samples, num_shards, input_shards, _ = sample_prop(
            sizefilepath, input_shards, proportion, is_local=is_local
        )
    else:
        num_samples, num_shards = get_dataset_size(
            input_shards, sizefilepath_=sizefilepath_, is_local=is_local
        )

    if not num_samples:
        if is_train:
            num_samples = args.train_num_samples
            if not num_samples:
                raise RuntimeError(
                    "Currently, number of dataset samples must be specified for training dataset. "
                    "Please specify via `--train-num-samples` if no dataset length info present."
                )
        else:
            num_samples = (
                args.val_num_samples or 0
            )  # eval will just exhaust the iterator if not specified

    pipeline = [wds.SimpleShardList(input_shards)]
    # at this point we have an iterator over all the shards
    if is_train:
        pipeline.extend(
            [
                wds.detshuffle(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                ),
                wds.split_by_node,
                wds.split_by_worker,
                # at this point, we have an iterator over the shards assigned to each worker at each node
                wds.tarfile_to_samples(handler=log_and_continue),
                wds.shuffle(
                    bufsize=_SAMPLE_SHUFFLE_SIZE,
                    initial=_SAMPLE_SHUFFLE_INITIAL,
                    rng=random.Random(args.seed),
                ),
                # wds.repeatedly,  # FIXME determine if this is beneficial
            ]
        )
    else:
        pipeline.extend(
            [
                wds.split_by_worker,
                # at this point, we have an iterator over the shards assigned to each worker
                wds.tarfile_to_samples(handler=log_and_continue),
            ]
        )
    pipeline.extend(
        [
            wds.map(
                partial(
                    preprocess,
                    audio_ext=audio_ext,
                    text_ext=text_ext,
                    samplerate=samplerate,
                    mono=mono,
                    max_len=max_len,
                    dtype=dtype,
                    res_type=res_type,
                    resample_method=args.resample_method,
                )
            ),
            # TODO: (yusong) use wds.to_dict instead?
            wds.to_tuple(
                "__url__",
                "__key__",
                "waveform",
                "text",
                "raw_text",
                "audio_name",
                "text_name",
                "audio_orig_sr",
            ),
            wds.batched(args.batch_size, partial=not is_train),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if is_train:
        # roll over and repeat a few samples to get same number of full batches on each node
        global_batch_size = args.batch_size * args.world_size
        num_batches = math.ceil(num_samples / global_batch_size)
        num_workers = max(1, args.workers)
        num_worker_batches = math.ceil(
            num_batches / num_workers
        )  # per dataloader worker
        num_batches = num_worker_batches * num_workers
        num_samples = num_batches * global_batch_size
        dataset = dataset.with_epoch(
            num_worker_batches
        )  # each worker is iterating over this
    else:
        # last batches are partial, eval is done on single (master) node
        num_batches = math.ceil(num_samples / args.batch_size)

    dataloader = wds.WebLoader(
        dataset, batch_size=None, shuffle=False, num_workers=args.workers
    )

    # FIXME not clear which approach is better, with_epoch before vs after dataloader?
    # hoping to resolve via https://github.com/webdataset/webdataset/issues/169
    # if is_train:
    #     # roll over and repeat a few samples to get same number of full batches on each node
    #     global_batch_size = args.batch_size * args.world_size
    #     num_batches = math.ceil(num_samples / global_batch_size)
    #     num_workers = max(1, args.workers)
    #     num_batches = math.ceil(num_batches / num_workers) * num_workers
    #     num_samples = num_batches * global_batch_size
    #     dataloader = dataloader.with_epoch(num_batches)
    # else:
    #     # last batches are partial, eval is done on single (master) node
    #     num_batches = math.ceil(num_samples / args.batch_size)

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader, None)


def wds_batch_list2dict(
    batch,
    keys=[
        "__url__",
        "__key__",
        "waveform",
        "text",
        "raw_text",
        "audio_name",
        "text_name",
        "audio_orig_sr",
    ],
):
    """
    Return a dictionary of the batch, with keys as the names of the fields.
    """
    assert len(keys) == len(
        batch
    ), "batch must have same number of keys as keys argument"
    return {keys[i]: batch[i] for i in range(len(batch))}


def get_csv_dataset(args, preprocess_fn, is_train):
    input_filename = args.train_data if is_train else args.val_data
    assert input_filename
    dataset = CsvDataset(
        input_filename,
        preprocess_fn,
        img_key=args.csv_img_key,
        caption_key=args.csv_caption_key,
        sep=args.csv_separator,
    )
    num_samples = len(dataset)
    sampler = DistributedSampler(dataset) if args.distributed and is_train else None
    shuffle = is_train and sampler is None

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=shuffle,
        num_workers=args.workers,
        pin_memory=True,
        sampler=sampler,
        drop_last=is_train,
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


def get_toy_dataset(args, model_cfg, is_train):
    index_path = args.train_data if is_train else args.val_data
    ipc_path = args.train_ipc if is_train else args.val_ipc
    assert index_path and ipc_path
    eval_mode = not is_train
    dataset = ToyDataset(index_path, ipc_path, model_cfg, eval_mode=eval_mode)

    num_samples = len(dataset)
    sampler = (
        DistributedSampler(dataset, shuffle=False)
        if args.distributed and is_train
        else None
    )

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        sampler=sampler,
        drop_last=is_train,
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


def get_dataset_fn(data_path, dataset_type):
    if dataset_type == "webdataset":
        return get_wds_dataset
    elif dataset_type == "csv":
        return get_csv_dataset
    elif dataset_type == "auto":
        ext = data_path.split(".")[-1]
        if ext in ["csv", "tsv"]:
            return get_csv_dataset
        elif ext in ["tar"]:
            return get_wds_dataset
        else:
            raise ValueError(
                f"Tried to figure out dataset type, but failed for extention {ext}."
            )
    elif dataset_type == "toy":
        return get_toy_dataset
    else:
        raise ValueError(f"Unsupported dataset type: {dataset_type}")


def get_data(args, model_cfg):
    # deprecated for audio
    # preprocess_train, preprocess_val = preprocess_fns
    data = {}

    # need to CHANGE when using the formal webdataset
    # if args.train_data:
    #     data["train"] = get_dataset_fn(args.train_data, args.dataset_type)(
    #         args, preprocess_train, is_train=True
    #     )

    # if args.val_data:
    #     data["val"] = get_dataset_fn(args.val_data, args.dataset_type)(
    #         args, preprocess_val, is_train=False
    #     )

    # if args.imagenet_val is not None:
    #     data["imagenet-val"] = get_imagenet(args, preprocess_fns, "val")

    # if args.imagenet_v2 is not None:
    #     data["imagenet-v2"] = get_imagenet(args, preprocess_fns, "v2")
    if args.train_data:
        data["train"] = get_dataset_fn(args.train_data, args.dataset_type)(
            args, model_cfg, is_train=True
        )

    if args.val_data:
        data["val"] = get_dataset_fn(args.val_data, args.dataset_type)(
            args, model_cfg, is_train=False
        )

    return data
