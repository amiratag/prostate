import sys
import os
import argparse
import logging
import shutil
import re
from PIL import Image
from skimage import io
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from tqdm import tqdm
import torch
from torch import nn
import torch.nn.functional as F
import torchvision.models as models
import torch.optim as optim
from torchvision.datasets import VisionDataset
from torchvision import transforms
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from sklearn.model_selection import train_test_split

def checkpoint_epoch(checkpoint_name):
    
    return int(checkpoint_name.split('/')[-1].split('.')[-2].split('-')[-1].replace('epoch', ''))


def list_checkpoints(save_path, model_name=''):
    
    return [os.path.join(save_path, i) for i in os.listdir(save_path)
            if '.pt' in i and 'opt-' not in i and model_name in i]

def delete_old_ckpts(save_path, model_name='', num_save=3):
    
    checkpoints = list_checkpoints(save_path, model_name)
    saved_epochs = [checkpoint_epoch(checkpoint) for checkpoint in checkpoints]
    [os.remove(checkpoint)
     for checkpoint in checkpoints
     if checkpoint_epoch(checkpoint) in np.sort(saved_epochs)[:-num_save]]
    [os.remove(checkpoint.replace('checkpoint-', 'opt-checkpoint_').replace('.pt', '.tar'))
     for checkpoint in checkpoints
     if checkpoint_epoch(checkpoint) in np.sort(saved_epochs)[:-num_save]]

class CustomDataset(VisionDataset):
    
    def __init__(self, X, y, train=True, transform=None, target_transform=None, download=None):
        
        super(CustomDataset, self).__init__('', transform=transform,
                                      target_transform=target_transform)
        self.train = train
        self.X = X
        self.y = y
        assert len(self.X) == len(self.y)
        
    def __len__(self):
        return len(self.X)
    
    def __getitem__(self, index):
        
        img, target = self.X[index], self.y[index]
        img = np.array(Image.open(img))
        if self.transform is not None:
            img = self.transform(img)
        if self.target_transform is not None:
            target = self.target_transform(target)
        return img, target
    
    def extra_repr(self):
        return "Split: {}".format("Train" if self.train is True else "Test")
    
class ContrastiveDataset(VisionDataset):
    
    def __init__(self, X, n_views, transform):
        
        super().__init__('', transform=transform)
        self.X = X
        self.n_views = n_views
        self.transform = transform
        
    def __len__(self):
        return len(self.X)
    
    def __getitem__(self, index):
        
        img = self.X[index]
        return [self.transform(img) for i in range(self.n_views)]
    
class DenseNet(torch.nn.Module):
    
    def __init__(self, num_classes, pretrained=True):
        super().__init__()
        self.feature_extractor = models.densenet121(pretrained=pretrained)
        self.final = torch.nn.Linear(1000, num_classes)
    
    def forward(self, x):
        x = self.feature_extractor(x)
        return self.final(x)
    
def balanced_dataset(X, y, max_size=None, min_size=None):
    
    np.random.seed(0)
    counts = np.bincount(y)
    labels, counts = np.unique(y, return_counts=True)
    minority = np.min(counts)
    if min_size is not None:
        minority = max(minority, min_size // len(labels))
    idxs = []
    for label in labels:
        label_idxs = np.where(y == label)[0]
        if len(label_idxs) >= minority:
            idxs.extend(np.random.choice(label_idxs, minority, replace=False))
        else:
            idxs.extend(np.random.choice(label_idxs, minority, replace=True))
    idxs = np.random.permutation(idxs)
    if max_size:
        idxs = idxs[:max_size]
    return X[idxs], y[idxs]

def adjust_learning_rate(args, optimizer, epoch):
    """decrease the learning rate"""
    lr = args.lr
    schedule = args.lr_schedule
    # schedule from TRADES repo (different from paper due to bug there)
    if schedule == 'trades':
        if epoch >= 0.75 * args.epochs:
            lr = args.lr * 0.1
    # schedule as in TRADES paper
    elif schedule == 'trades_fixed':
        if epoch >= 0.75 * args.epochs:
            lr = args.lr * 0.1
        if epoch >= 0.9 * args.epochs:
            lr = args.lr * 0.01
        if epoch >= args.epochs:
            lr = args.lr * 0.001
    # cosine schedule
    elif schedule == 'cosine':
        lr = args.lr * 0.5 * (1 + np.cos((epoch - 1) / args.epochs * np.pi))
    # schedule as in WRN paper
    elif schedule == 'wrn':
        if epoch >= 0.3 * args.epochs:
            lr = args.lr * 0.2
        if epoch >= 0.6 * args.epochs:
            lr = args.lr * 0.2 * 0.2
        if epoch >= 0.8 * args.epochs:
            lr = args.lr * 0.2 * 0.2 * 0.2
    else:
        raise ValueError('Unkown LR schedule %s' % schedule)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr    


class GaussianBlur(object):
    """blur a single image on CPU"""
    def __init__(self, kernel_size):
        radias = kernel_size // 2
        kernel_size = radias * 2 + 1
        self.blur_h = nn.Conv2d(3, 3, kernel_size=(kernel_size, 1),
                                stride=1, padding=0, bias=False, groups=3)
        self.blur_v = nn.Conv2d(3, 3, kernel_size=(1, kernel_size),
                                stride=1, padding=0, bias=False, groups=3)
        self.k = kernel_size
        self.r = radias

        self.blur = nn.Sequential(
            nn.ReflectionPad2d(radias),
            self.blur_h,
            self.blur_v
        )

        self.pil_to_tensor = transforms.ToTensor()
        self.tensor_to_pil = transforms.ToPILImage()

    def __call__(self, img):
        img = self.pil_to_tensor(img).unsqueeze(0)

        sigma = np.random.uniform(0.1, 2.0)
        x = np.arange(-self.r, self.r + 1)
        x = np.exp(-np.power(x, 2) / (2 * sigma * sigma))
        x = x / x.sum()
        x = torch.from_numpy(x).view(1, -1).repeat(3, 1)

        self.blur_h.weight.data.copy_(x.view(3, 1, self.k, 1))
        self.blur_v.weight.data.copy_(x.view(3, 1, 1, self.k))

        with torch.no_grad():
            img = self.blur(img)
            img = img.squeeze()

        img = self.tensor_to_pil(img)

        return img
    
    
class ResNetSimCLR(nn.Module):

    def __init__(self, base_model, out_dim):
        super().__init__()
        print(base_model)
        if base_model == 'resnet18':
            self.backbone = models.resnet18(pretrained=False, num_classes=out_dim)
        elif base_model == 'resnet34':
            self.backbone = models.resnet34(pretrained=False, num_classes=out_dim)
        elif base_model == 'resnet50':
            self.backbone = models.resnet50(pretrained=False, num_classes=out_dim)
        else:
            raise ValueError('Invalid base_model!')
        dim_mlp = self.backbone.fc.in_features

        # add mlp projection head
        self.backbone.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.backbone.fc)

    def forward(self, x):
        return self.backbone(x)
