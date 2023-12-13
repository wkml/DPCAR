import sys
import time
import random
import numpy as np
from datetime import datetime

from loguru import logger
import hydra
from omegaconf import DictConfig, OmegaConf

from tensorboardX import SummaryWriter

import torch
from torch import nn
import torch.nn.functional as F
import torch.optim
import torch.optim.lr_scheduler as lr_scheduler 
from torch.cuda.amp import autocast, GradScaler

from model.SSGRL import SSGRL, update_feature, compute_prototype
from loss import InstanceContrastiveLoss, PrototypeContrastiveLoss
from calibration.Calibration import MDCA, FocalLoss, FLSD, DCA, MbLS, DWBL, MMCE

from utils.dataloader import get_graph_and_word_file, get_data_loader
from utils.metrics import AverageMeter, AveragePrecisionMeter, Compute_mAP_VOC2012
from utils.checkpoint import save_checkpoint
from utils.label_smoothing import label_smoothing_tradition, label_smoothing_dynamic

import warnings

warnings.filterwarnings('ignore')

global best_prec
best_prec = 0

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

@hydra.main(version_base=None, config_path='./config/', config_name="config")
def main(cfg: DictConfig):
    global best_prec

    # Argument Parse
    cfg.post = f"{cfg.model.name}-{cfg.dataset.name}-{cfg.model.method}-eps{cfg.model.eps}".replace('.', '_')
    cfg.post = str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))[:10] + '-' + cfg.post

    if cfg.seed is not None:
        logger.info('absolute seed: {}'.format(cfg.seed))
        random.seed(cfg.seed)
        np.random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)
        torch.cuda.manual_seed(cfg.seed)

    # Bulid Logger
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
    logger.add('exp/log/{}.log'.format(cfg.post), format=log_format, level="INFO")

    # Show Argument
    logger.info("==========================================")
    logger.info("==========       CONFIG      =============")
    logger.info("==========================================")

    logger.info('\n{}'.format(OmegaConf.to_yaml(cfg)))

    logger.info("==========================================")
    logger.info("===========        END        ============")
    logger.info("==========================================")

    logger.info("\n")

    # Create dataloader
    logger.info("==> Creating dataloader...")
    train_loader, test_loader = get_data_loader(cfg)
    logger.info("==> Done!\n")

    # Load the network
    logger.info("==> Loading the network...")
    graph_file, word_file = get_graph_and_word_file(cfg, train_loader.dataset.changed_labels)
    model = SSGRL(graph_file, word_file, class_nums=cfg.dataset.class_nums)
    aux_model = SSGRL(graph_file, word_file, class_nums=cfg.dataset.class_nums)
    scaler = GradScaler()

    if cfg.model.resume_model != 'None':
        logger.info("==> Loading checkpoint...")
        checkpoint = torch.load(cfg.model.resume_model, map_location='cpu')
        best_prec, cfg.start_epoch = checkpoint['best_mAP'], checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])
        logger.info("==> Checkpoint Epoch: {0}, mAP: {1}".format(cfg.start_epoch, best_prec))
    
    if cfg.model.aux_model != 'None':
        logger.info("==> Loading auxiliary model...")
        checkpoint = torch.load(cfg.model.aux_model, map_location='cpu')
        aux_model.load_state_dict(checkpoint['state_dict'])

        aux_model.to(device)
        aux_model.eval()
        for p in aux_model.parameters():
            p.requires_grad = False

    for p in model.backbone.parameters():
        p.requires_grad = True

    model.to(device)
    
    logger.info("==> Done!\n")

    criterion = {'BCEWithLogitsLoss': nn.BCEWithLogitsLoss(reduce=True, size_average=True).to(device),
                 'InterInstanceDistanceLoss': InstanceContrastiveLoss(cfg.batch_size, reduce=True, size_average=True).to(device),
                 'InterPrototypeDistanceLoss': PrototypeContrastiveLoss(reduce=True, size_average=True).to(device),
                 'MDCA': MDCA().to(device),
                 'FocalLoss': FocalLoss().to(device),
                 'FLSD': FLSD().to(device),
                 'DCA': DCA().to(device),
                 'MbLS': MbLS().to(device),
                 'DWBL': DWBL().to(device),
                 'MMCE': MMCE().to(device),
                 }

    optimizer = torch.optim.Adam(filter(lambda p : p.requires_grad, model.parameters()), lr=cfg.lr)

    scheduler = lr_scheduler.StepLR(optimizer, step_size=cfg.step_epoch, gamma=0.1)

    if cfg.evaluate:
        Validate(test_loader, model, criterion, 0, cfg)
        return
    
    # Running Experiment
    logger.info("Run Experiment...")
    writer = SummaryWriter('exp/summary/{}'.format(str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))[:10] + '-' + cfg.post))

    for epoch in range(cfg.start_epoch, cfg.start_epoch + cfg.epochs):

        if (cfg.model.method == 'DPCAR' or cfg.model.method == 'PROTOTYPE') \
            and epoch >= cfg.model.generate_label_epoch \
            and epoch % cfg.model.compute_prototype_epoch == 0:
            
            if epoch == cfg.model.generate_label_epoch or cfg.model.use_recompute_prototype:
                logger.info('Compute Prototype...')
                compute_prototype(model, train_loader, cfg)
                logger.info('Done!\n')
        
        if cfg.model.method == 'DPCAR_AUX':
            logger.info('Compute Prototype...')
            compute_prototype(aux_model, train_loader, cfg)
            logger.info('Done!\n')

        Train(train_loader, model, aux_model, criterion, optimizer, writer, epoch, cfg, scaler)
        mAP, ACE, ECE, MCE = Validate(test_loader, model, criterion, epoch, cfg)

        scheduler.step()

        writer.add_scalar('mAP', mAP, epoch)
        writer.add_scalar('ACE', ACE, epoch)
        writer.add_scalar('ECE', ECE, epoch)
        writer.add_scalar('MCE', MCE, epoch)

        isBest, best_prec = mAP > best_prec, max(mAP, best_prec)
        save_checkpoint(cfg, {'epoch':epoch, 'state_dict': model.state_dict(), 'best_mAP': mAP}, isBest)

        if isBest:
            logger.info('[Best] [Epoch {0}]: Best mAP is {1:.3f}'.format(epoch, best_prec))

    writer.close()

def Train(train_loader, model, aux_model, criterion, optimizer, writer, epoch, cfg, scaler):
    optimizer.zero_grad()
    model.train()

    loss, loss_base, loss_plus, loss_calibration = AverageMeter(), AverageMeter(), AverageMeter(), AverageMeter()
    batch_time, data_time = AverageMeter(), AverageMeter()
    logger.info("=========================================")

    end = time.time()
    for batch_index, (sample_index, input, target, full_label, mask) in enumerate(train_loader):
        """
            target = [-1, 0, 1]
            target_ = [0, 1]
        """
        input, target = input.to(device), target.float().to(device)

        # Log time of loading data
        data_time.update(time.time() - end)

        # Forward
        outputs, semantic_feature = model(input)
        if cfg.model.method == 'DPCAR_AUX':
            _, _, aux_feature = aux_model(input)

        # Label Smoothing
        if cfg.model.method == 'label_smoothing':
            target_ = label_smoothing_tradition(cfg, full_label)

        elif cfg.model.method == 'prototype':
            target_ = label_smoothing_dynamic(cfg, full_label, model.prototype, semantic_feature, epoch)
            
        elif cfg.model.method == 'instance':
            update_feature(model, semantic_feature, target, cfg.model.inter_example_nums)
            target_ = label_smoothing_dynamic(cfg, full_label, model.pos_feature, semantic_feature, epoch)

        elif cfg.model.method == 'DPCAR':
            update_feature(model, semantic_feature, target, cfg.model.inter_example_nums)
            target_instance = label_smoothing_dynamic(cfg, full_label, model.pos_feature, semantic_feature, epoch, 10)
            target_prototype = label_smoothing_dynamic(cfg, full_label, model.prototype, semantic_feature, epoch, 10)
        
        elif cfg.model.method == 'DPCAR_AUX':
            update_feature(aux_model, aux_feature, target, cfg.model.inter_example_nums)
            target_instance = label_smoothing_dynamic(cfg, full_label, aux_model.pos_feature, aux_feature, epoch, 10)
            target_prototype = label_smoothing_dynamic(cfg, full_label, aux_model.prototype, aux_feature, epoch, 10)

        else:
            # Non Label Smoothing
            target_ = target.detach().clone().to(device)
            target_[target_ < 0] = 0

        # Loss
        if cfg.model.method == 'DPCAR':
            # with autocast():
            loss_instance = criterion['BCEWithLogitsLoss'](outputs, target_instance)
            loss_prototype = criterion['BCEWithLogitsLoss'](outputs, target_prototype)
            loss_base_ = (loss_instance + loss_prototype) / 2

            # warm up
            loss_plus_ = cfg.model.inter_distance_weight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) if epoch >= 1 else \
                    cfg.model.inter_distance_weight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) * batch_index / float(len(train_loader))

            loss_calibration_ = torch.tensor(0.0).to(device)

        elif cfg.model.method == 'DPCAR_AUX':
            loss_instance = criterion['BCEWithLogitsLoss'](outputs, target_instance)
            loss_prototype = criterion['BCEWithLogitsLoss'](outputs, target_prototype)
            loss_base_ = (loss_instance + loss_prototype) / 2

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = torch.tensor(0.0).to(device)

        elif cfg.model.method == 'instance' or cfg.model.method == 'PROTOTYPE':
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            loss_plus_ = cfg.model.inter_distance_weight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) if epoch >= 1 else \
                     cfg.model.inter_distance_weight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) * batch_index / float(len(train_loader))

            loss_calibration_ = torch.tensor(0.0).to(device)

        elif cfg.model.method == 'FL':
            loss_base_ = criterion['FocalLoss'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = torch.tensor(0.0).to(device)
        
        elif cfg.model.method == 'FLSD':
            loss_base_ = criterion['FLSD'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = torch.tensor(0.0).to(device)

        elif cfg.model.method == 'MDCA':
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = criterion['MDCA'](outputs, target_)
        
        elif cfg.model.method == 'DCA':
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = criterion['DCA'](outputs, target_)
        
        elif cfg.model.method == 'MbLS':
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = criterion['MbLS'](outputs, target_)
        
        elif cfg.model.method == 'DWBL':
            loss_base_ = criterion['DWBL'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = torch.tensor(0.0).to(device)
        
        elif cfg.model.method == 'MMCE':
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = criterion['MMCE'](outputs, target_)

        else:
            loss_base_ = criterion['BCEWithLogitsLoss'](outputs, target_)

            # loss_plus_ = args.interDistanceWeight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) if epoch >= 1 else \
            #          args.interDistanceWeight * criterion['InterInstanceDistanceLoss'](semantic_feature, target) * batchIndex / float(len(train_loader))

            loss_plus_ = torch.tensor(0.0).to(device)

            loss_calibration_ = torch.tensor(0.0).to(device)
            
        loss_ = loss_base_ + loss_plus_ + loss_calibration_

        loss.update(loss_.item(), input.size(0))
        loss_base.update(loss_base_.item(), input.size(0))
        loss_plus.update(loss_plus_.item(), input.size(0))
        loss_calibration.update(loss_calibration_.item(), input.size(0))

        # Backward
        loss_.backward()
        optimizer.step()
        optimizer.zero_grad()
        # scaler.scale(loss_).backward()
        # scaler.step(optimizer)
        # scaler.update()
        
        # Log time of batch
        batch_time.update(time.time() - end)
        end = time.time()

        if batch_index % cfg.print_freq == 0:
            lr = optimizer.param_groups[0]['lr']
            logger.info(f'\n\t\t\t\t\t\t[Train] [Epoch {epoch}]: [{batch_index:04d}/{len(train_loader)}] Batch Time {batch_time.avg:.3f} Data Time {data_time.avg:.3f}\n'
                        f'\t\t\t\t\t\tLearn Rate {lr:.6f}\n'
                        f'\t\t\t\t\t\tBase Loss {loss_base.val:.4f} ({loss_base.avg:.4f})\n'
                        f'\t\t\t\t\t\tPlus Loss {loss_plus.val:.4f} ({loss_plus.avg:.4f})\n'
                        f'\t\t\t\t\t\tCalibration Loss {loss_calibration.val:.4f} ({loss_calibration.avg:.4f})')
            sys.stdout.flush()

    writer.add_scalar('Loss', loss.avg, epoch)
    writer.add_scalar('Loss_Base', loss_base.avg, epoch)
    writer.add_scalar('Loss_Plus', loss_plus.avg, epoch)
    writer.add_scalar('Loss_Calibration', loss_calibration.avg, epoch)

def Validate(val_loader, model, criterion, epoch, cfg):

    model.eval()

    apMeter = AveragePrecisionMeter()
    pred, loss, batch_time, data_time = [], AverageMeter(), AverageMeter(), AverageMeter()
    logger.info("=========================================")

    end = time.time()
    for batchIndex, (sample_index, input, target, full_label, mask) in enumerate(val_loader):

        input, target = input.to(device), target.float().to(device)
        
        # Log time of loading data
        data_time.update(time.time() - end)

        # Forward
        with torch.no_grad():
            output, semantic_feature = model(input)

        target[target < 0] = 0

        # Compute loss and prediction
        loss_ = criterion['BCEWithLogitsLoss'](output, target)
        loss.update(loss_.item(), input.size(0))

        # Change target to [0, 1]
        # target[target < 0] = 0

        apMeter.add(output, target)
        pred.append(torch.cat((output, (target > 0).float()), 1))

        # Log time of batch
        batch_time.update(time.time() - end)
        end = time.time()

        # logger.info information of current batch        
        if batchIndex % cfg.print_freq == 0:
            logger.info('[Test] [Epoch {0}]: [{1:04d}/{2}] '
                        'Batch Time {batch_time.avg:.3f} Data Time {data_time.avg:.3f} '
                        'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                epoch, batchIndex, len(val_loader),
                batch_time=batch_time, data_time=data_time,
                loss=loss))
            sys.stdout.flush()

    pred = torch.cat(pred, 0).cpu().clone().numpy()
    mAP = Compute_mAP_VOC2012(pred, cfg.dataset.class_nums)

    averageAP = apMeter.value().mean()
    OP, OR, OF1, CP, CR, CF1 = apMeter.overall()
    OP_K, OR_K, OF1_K, CP_K, CR_K, CF1_K = apMeter.overall_topk(3)
    ACE, ECE, MCE = apMeter.calibration()
    mACE, mECE, mMCE = apMeter.compute_classwise()

    logger.info(f'\n\t\t\t\t[Test] mAP: {mAP:.3f}, averageAP: {averageAP:.3f}\n'
                f'\t\t\t\t(Compute with all label) OP: {OP:.3f}, OR: {OR:.3f}, OF1: {OF1:.3f}, CP: {CP:.3f}, CR: {CR:.3f}, CF1:{CF1:.3f}\n'
                f'\t\t\t\t(Compute with top-3 label) OP: {OP_K:.3f}, OR: {OR_K:.3f}, OF1: {OF1_K:.3f}, CP: {CP_K:.3f}, CR: {CR_K:.3f}, CF1: {CF1_K:.3f}\n'
                f'\t\t\t\tACE:{ACE:.6f}, ECE:{ECE:.6f}, MCE:{MCE:.6f}\n'
                f'\t\t\t\tmACE:{mACE:.6f}, mECE:{mECE:.6f}, mMCE:{mMCE:.6f}')

    return mAP, ACE, ECE, MCE


if __name__=="__main__":
    main()
