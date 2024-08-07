import os
import logging
import numpy as np
from tqdm import tqdm
import torch
from torch import optim
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel import DataParallel
from sklearn.metrics import accuracy_score
import argparse
import wandb
from model.model import MMC
from model.model_cross import MMC_Cross
from data.dataloader import MMDataLoader
from src.metrics import collect_metrics
from src.functions import save_checkpoint, load_checkpoint, dict_to_str, count_parameters
from src.config import Config

import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')

__all__ = ['TrainModule']

torch.cuda.current_device()


os.environ['TORCH_USE_CUDA_DSA'] = '1'

wandb.init(project='MMC')

parser = argparse.ArgumentParser()

parser.add_argument('--name', type=str, default='MMC', help='project name')
parser.add_argument('--dataset', type=str, default='Food101', help='support N24News/Food101')
parser.add_argument('--text_type', type=str, default='headline', help='support headline/caption/abstract')
parser.add_argument('--mmc', type=str, default='UniSMMC', help='support UniSMMC/UnSupMMC/SupMMC')
parser.add_argument('--mmc_tao', type=float, default=0.07, help='use supervised contrastive loss or not')
parser.add_argument('--batch_size', type=int, default=32, help='batch_size')
parser.add_argument('--lr_mm', type=float, default=1e-3, help='--lr_mm')
parser.add_argument('--min_epoch', type=int, default=1, help='min_epoch')    
parser.add_argument('--valid_step', type=int, default=50, help='valid_step')              
parser.add_argument('--max_length', type=int, default=512, help='max_length')

parser.add_argument('--text_encoder', type=str, default='bert_base', help='bert_base/roberta_base/bert_large')
parser.add_argument('--image_encoder', type=str, default='vit_base', help='vit_base/vit_large')

parser.add_argument('--text_out', type=int, default=768, help='text_out')
parser.add_argument('--img_out', type=int, default=768, help='img_out')    

parser.add_argument('--post_dim', type=int, default=256, help='post_dim')
parser.add_argument('--output_dim', type=int, default=101, help='output_dim')

parser.add_argument('--lr_text_tfm', type=float, default=5e-5, help='--lr_text_tfm')
parser.add_argument('--lr_img_tfm', type=float, default=5e-5, help='--lr_img_tfm')
parser.add_argument('--lr_text_cls', type=float, default=1e-4,help='--lr_text_cls')
parser.add_argument('--lr_img_cls', type=float, default=1e-4, help='--lr_img_cls')
parser.add_argument('--lr_mm_cls', type=float, default=1e-4, help='--lr_mm_cls')

parser.add_argument('--text_dropout', type=float, default=0.1, help='--text_dropout')
parser.add_argument('--img_dropout', type=float, default=0.1, help='--img_dropout')
parser.add_argument('--mm_dropout', type=float, default=0.0, help='--mm_dropout')
parser.add_argument('--nplot', type=str, default='', help='MTAV')
parser.add_argument('--data_dir', type=str, default='./datasets/', help='support wmsa') 
parser.add_argument('--test_only', type=bool, default=False,  help='train+test or test only')
parser.add_argument('--pretrained_dir', type=str, default='./pretrained_models', help='path to pretrained models from Hugging Face.')
parser.add_argument('--model_save_dir', type=str, default='Path/To/results/models', help='path to save model parameters.')
parser.add_argument('--res_save_dir', type=str, default='Path/To/results/results', help='path to save training results.')
parser.add_argument('--fig_save_dir', type=str, default='Path/To/results/imgs', help='path to save figures.')
parser.add_argument('--logs_dir', type=str, default='Path/To/results/logs', help='path to log results.')  # NO
parser.add_argument('--local_rank', default=-1, type=int, help='node rank for distributed training')
parser.add_argument('--seeds', nargs='+', type=int, help='set seeds for multiple runs!')
parser.add_argument('--model_path', type=str, default='./Path/To/results/models', help='path to load model parameters')
parser.add_argument('--save_model', type=bool, default=True, help='save model or not')
parser.add_argument('--cross_attention', action='store_true', help='cross attention or not')
parser.add_argument('--num_epoch', type=int, default=25, help='num_epoch')
parser.add_argument('--seed', type=int, default=42, help='seed')
parser.add_argument('--lr_patience', type=int, default=3, help='lr_patience')
parser.add_argument('--lr_factor', type=float, default=0.2, help='lr_factor')
# parser.add_argument('--weight_decay_tfm', type=float, default=0.001, help='weight_decay_tfm')
# parser.add_argument('--weight_decay_other', type=float, default=0.0001, help='weight_decay_other')
# parser.add_argument('--batch_gradient', type=int, default=128, help='batch_gradient')
# parser.add_argument('--num_workers', type=int, default=24, help='num_workers')
# parser.add_argument('--post_dim', type=int, default=256, help='post_dim')
# parser.add_argument('--output_dim', type=int, default=101, help='output_dim')

torch.autograd.set_detect_anomaly(True)
torch.cuda.empty_cache()

args = parser.parse_args()
config = Config(args)
args = config.get_config()

np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)

if args.local_rank == -1:
    device = torch.device("cuda")
else:
    torch.cuda.set_device(args.local_rank)
    device = torch.device("cuda", args.local_rank)
    torch.distributed.init_process_group(backend="nccl")

args.device = device
args.data_dir = os.path.join(args.data_dir, args.dataset)

wandb.config.update(args)

print(args)


# # To decide the lr scheduler
# def get_scheduler(optimizer, args):
#     return optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.num_epoch)

# To decide the lr scheduler
def get_scheduler(optimizer, args):
    return optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, "max", patience=args.lr_patience, verbose=True, factor=args.lr_factor
    )

# To decide the optimizer
def get_optimizer(model, args):
    if args.mmc not in ['V']: #Goes in both if conditions with train_food.py

        text_enc_param = list(model.module.text_encoder.named_parameters())
        text_clf_param = list(model.module.text_classfier.parameters())
    if args.mmc not in ['T']:

        img_enc_param = list(model.module.image_encoder.parameters())
        img_clf_param = list(model.module.image_classfier.parameters())
    mm_clf_param = list(model.module.mm_classfier.parameters())

    print(type(img_enc_param[1]))
    print(type(img_clf_param[1]))   
    print(type(text_enc_param[1]))
    print(type(text_clf_param[1]))
    print(type(mm_clf_param[1]))

    no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
    if args.mmc in ['V']:
        optimizer_grouped_parameters = [
            {"params": img_enc_param, "weight_decay": args.weight_decay_tfm, 'lr': args.lr_img_tfm},
            {"params": img_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_img_cls},
            {"params": mm_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_mm_cls},
        ]
    elif args.mmc in ['T']:
        optimizer_grouped_parameters = [
            {"params": [p for n, p in text_enc_param if not any(nd in n for nd in no_decay)],
             "weight_decay": args.weight_decay_tfm, 'lr': args.lr_text_tfm},
            {"params": [p for n, p in text_enc_param if any(nd in n for nd in no_decay)], "weight_decay": 0.0,
             'lr': args.lr_text_tfm},
            {"params": text_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_text_cls},
            {"params": mm_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_mm_cls},
        ]
    else:
        optimizer_grouped_parameters = [
            {"params": [p for n, p in text_enc_param if not any(nd in n for nd in no_decay)],
             "weight_decay": args.weight_decay_tfm, 'lr': args.lr_text_tfm},
            {"params": [p for n, p in text_enc_param if any(nd in n for nd in no_decay)], "weight_decay": 0.0,
             'lr': args.lr_text_tfm},
            {"params": text_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_text_cls},
            {"params": img_enc_param, "weight_decay": args.weight_decay_tfm, 'lr': args.lr_img_tfm},
            {"params": img_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_img_cls},
            {"params": mm_clf_param, "weight_decay": args.weight_decay_other, 'lr': args.lr_mm_cls},
        ]
    optimizer = optim.Adam(optimizer_grouped_parameters)

    return optimizer


def main():

    train_loader, valid_loader, test_loader = MMDataLoader(args)

    # print number of train, valid, test samples
    print(f"Train: {len(train_loader.dataset)}, Valid: {len(valid_loader.dataset)}, Test: {len(test_loader.dataset)}")

            

    if args.local_rank in [-1]:
        if args.cross_attention:
            model = DataParallel(MMC_Cross(args))
            model = model.to(args.device)
        else:
            model = DataParallel(MMC(args))
            model = model.to(args.device)
    else:
        if args.cross_attention:
            model = MMC_Cross(args).to(args.device)
            model = nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                    output_device=args.local_rank,
                                                    find_unused_parameters=True)
        else:              
            model = MMC(args).to(args.device)
            model = nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                        output_device=args.local_rank,
                                                        find_unused_parameters=True)
        
    if args.local_rank in [-1, 0]:
        print(f'\nThe model has {count_parameters(model)} trainable parameters')

    optimizer = get_optimizer(model, args)
    scheduler = get_scheduler(optimizer, args)
    

    train_loss_m = 0
    gradient_accumulation_steps = int(args.batch_gradient / args.batch_size)

    accuracy_test_history = []

    for epoch in range(args.num_epoch):
        
        running_loss = 0.0
        model.train()

        if args.local_rank not in [-1]:
            train_loader.sampler.set_epoch(epoch)

        for i, (batch_image, text_input_ids, text_token_type_ids, text_attention_mask, batch_label) in enumerate(train_loader):

                text = text_input_ids.to(args.device), text_token_type_ids.to(args.device), text_attention_mask.to(args.device)
                image = batch_image.to(args.device)
                labels = batch_label.to(args.device).view(-1)

                loss, loss_m, logit_m = model(text, image, None, labels)
                # print(loss)
                loss = loss.sum() # / gradient_accumulation_steps
                loss.backward()

                if (i+1) % gradient_accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                
                running_loss += loss.detach().cpu().item()

                wandb.log({"training loss": loss})

        scheduler.step()

        print('[%d] training loss: %.3f' % (epoch + 1, running_loss / len(train_loader)))



        model.eval()
        
        with torch.no_grad():

            y_pred = []
            y_true = []

            for i, (batch_image, text_input_ids, text_token_type_ids, text_attention_mask, batch_label) in enumerate(test_loader):
                    
                text = text_input_ids.cuda(), text_token_type_ids.cuda(), text_attention_mask.cuda()
                image = batch_image.cuda()
                logit = model.module.infer(text, image, None)
                y_pred.append(logit.cpu())
                y_true.append(batch_label.cpu())

            logits = torch.cat(y_pred)
            true = torch.cat(y_true).data.cpu().numpy()
            prob = F.softmax(logits, dim=1).data.cpu().numpy()

            results = collect_metrics(args.dataset, true, prob)
            # get acc from results
            accuracy_test = results['acc']
            accuracy_test_history.append(accuracy_test)

            max_accuracy = max(accuracy_test_history)

            wandb.log({"test_acc": accuracy_test, "max_test_acc": max_accuracy})

            print(f'Accuracy: {accuracy_test} | Max Accuracy: {max_accuracy}')

    if args.save_model:
        save_checkpoint(model, args.best_model_save_path)

if __name__ == '__main__':
    main()


    