import os
import sys
import argparse
import logging
import itertools

import torch
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR, MultiStepLR

from detector.ssd.utils.misc import Timer
from detector.ssd.ssd import MatchPrior
from detector.ssd.mobilenetv3_ssd_lite import create_mobilenetv3_large_ssd_lite, create_mobilenetv3_small_ssd_lite
from detector.ssd.multibox_loss import MultiboxLoss

from dataset.voc import VOCDetection
from transform.collate import collate

import detector.ssd.config as config
from detector.ssd.data_preprocessing import TrainAugmentation, TestTransform


torch.multiprocessing.set_sharing_strategy('file_system')


def train(loader, net, criterion, optimizer, device, epoch=-1):
    net.train(True)

    running_loss = 0.0
    running_regression_loss = 0.0
    running_classification_loss = 0.0
    num = 0

    for i, data in enumerate(loader):
        images = data["image"]
        boxes = data["bboxes"]
        labels = data["category_id"]

        images = images.to(device)
        boxes = [b.to(device) for b in boxes]
        labels = [l.to(device) for l in labels]

        num += 1

        optimizer.zero_grad()
        confidence, locations = net(images)
        regression_loss, classification_loss = criterion(confidence, locations, labels, boxes)
        loss = regression_loss + classification_loss
        loss.backward()
        optimizer.step()

        running_loss += loss.item()
        running_regression_loss += regression_loss.item()
        running_classification_loss += classification_loss.item()

    avg_loss = running_loss / num
    avg_reg_loss = running_regression_loss / num
    avg_clf_loss = running_classification_loss / num

    logging.info(
        f"Epoch: {epoch}, Step: {i}, " +
        f"Average Loss: {avg_loss:.4f}, " +
        f"Average Regression Loss {avg_reg_loss:.4f}, " +
        f"Average Classification Loss: {avg_clf_loss:.4f}"
    )


def test(loader, net, criterion, device):
    net.eval()

    running_loss = 0.0
    running_regression_loss = 0.0
    running_classification_loss = 0.0
    num = 0

    for i, data in enumerate(loader):
        images = data["image"]
        boxes = data["bboxes"]
        labels = data["category_id"]

        images = images.to(device)
        boxes = [b.to(device) for b in boxes]
        labels = [l.to(device) for l in labels]

        num += 1

        with torch.no_grad():
            confidence, locations = net(images)
            regression_loss, classification_loss = criterion(confidence, locations, labels, boxes)
            loss = regression_loss + classification_loss

        running_loss += loss.item()
        running_regression_loss += regression_loss.item()
        running_classification_loss += classification_loss.item()

    return running_loss / num, running_regression_loss / num, running_classification_loss / num


def main():
    parser = argparse.ArgumentParser(
        description='Single Shot MultiBox Detector Training With Pytorch')

    parser.add_argument('--dataset', required=True, help='Dataset directory path')
    parser.add_argument('--validation-dataset', help='Dataset directory path')

    parser.add_argument('--net', default="mb3-small-ssd-lite",
                        help="The network architecture, it can be mb3-large-ssd-lite or mb3-small-ssd-lite.")

    # Params for SGD
    parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                        help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='Momentum value for optim')
    parser.add_argument('--weight-decay', default=5e-4, type=float,
                        help='Weight decay for SGD')
    parser.add_argument('--gamma', default=0.1, type=float,
                        help='Gamma update for SGD')

    # Params for loading pretrained basenet or checkpoints.
    parser.add_argument('--base-net',
                        help='Pretrained base model')

    # Scheduler
    parser.add_argument('--scheduler', default="multi-step", type=str,
                        help="Scheduler for SGD. It can one of multi-step and cosine")

    # Params for Multi-step Scheduler
    parser.add_argument('--milestones', default="80,100", type=str,
                        help="milestones for MultiStepLR")

    # Params for Cosine Annealing
    parser.add_argument('--t-max', default=120, type=float,
                        help='T_max value for Cosine Annealing Scheduler.')

    # Train params
    parser.add_argument('--batch-size', default=32, type=int,
                        help='Batch size for training')
    parser.add_argument('--num-epochs', default=120, type=int,
                        help='the number epochs')
    parser.add_argument('--num-workers', default=4, type=int,
                        help='Number of workers used in dataloading')
    parser.add_argument('--validation-epochs', default=5, type=int,
                        help='the number epochs')
    parser.add_argument('--use-cuda', default=True, type=bool,
                        help='Use CUDA to train model')

    parser.add_argument('--checkpoint-folder', default='output',
                        help='Directory for saving checkpoint models')


    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() and args.use_cuda else "cpu")

    if args.use_cuda and torch.cuda.is_available():
        logging.info("Use Cuda.")

    timer = Timer()

    logging.info(args)
    if args.net == 'mb3-large-ssd-lite':
        create_net = lambda num: create_mobilenetv3_large_ssd_lite(num)

    elif args.net == 'mb3-small-ssd-lite':
        create_net = lambda num: create_mobilenetv3_small_ssd_lite(num)

    else:
        logging.fatal("The net type is wrong.")
        parser.print_help(sys.stderr)
        sys.exit(1)

    train_transform = TrainAugmentation((config.image_size, config.image_size),
                                        config.image_mean, config.image_std,
                                        bbox_format='pascal_voc')

    test_transform = TestTransform((config.image_size, config.image_size),
                                   config.image_mean, config.image_std,
                                   bbox_format='pascal_voc')

    logging.info("Prepare training datasets.")

    dataset = VOCDetection(args.dataset, transform=train_transform)

    num_classes = len(dataset.class_names)

    logging.info("Train dataset size: {}".format(len(dataset)))

    train_loader = DataLoader(dataset, args.batch_size, collate_fn=collate,
                              num_workers=args.num_workers,
                              shuffle=True)

    logging.info("Prepare Validation datasets.")
    val_dataset = VOCDetection(args.validation_dataset, image_set="val",
                               transform=test_transform)
    logging.info("validation dataset size: {}".format(len(val_dataset)))

    val_loader = DataLoader(val_dataset, args.batch_size, collate_fn=collate,
                            num_workers=args.num_workers,
                            shuffle=False)

    logging.info("Build network.")
    net = create_net(num_classes)

    last_epoch = -1

    base_net_lr = args.lr
    extra_layers_lr = args.lr

    params = [
        {'params': net.base_net.parameters(), 'lr': base_net_lr},
        {'params': itertools.chain(
            net.extras.parameters()
        ), 'lr': extra_layers_lr},
        {'params': itertools.chain(
            net.regression_headers.parameters(),
            net.classification_headers.parameters()
        )}
    ]

    timer.start("Load Model")
    if args.base_net:
        logging.info(f"Init from base net {args.base_net}")
        net.init_from_base_net(args.base_net)
    logging.info(f'Took {timer.end("Load Model"):.2f} seconds to load the model.')

    net.to(device)

    criterion = MultiboxLoss(config.priors, iou_threshold=0.5, neg_pos_ratio=3,
                             center_variance=0.1, size_variance=0.2, device=device)
    optimizer = torch.optim.SGD(params, lr=args.lr, momentum=args.momentum,
                                weight_decay=args.weight_decay)
    logging.info(f"Learning rate: {args.lr}, Base net learning rate: {base_net_lr}, "
                 + f"Extra Layers learning rate: {extra_layers_lr}.")

    if args.scheduler == 'multi-step':
        logging.info("Uses MultiStepLR scheduler.")
        milestones = [int(v.strip()) for v in args.milestones.split(",")]
        scheduler = MultiStepLR(optimizer, milestones=milestones,
                                                     gamma=0.1, last_epoch=last_epoch)
    elif args.scheduler == 'cosine':
        logging.info("Uses CosineAnnealingLR scheduler.")
        scheduler = CosineAnnealingLR(optimizer, args.t_max, last_epoch=last_epoch)
    else:
        logging.fatal(f"Unsupported Scheduler: {args.scheduler}.")
        parser.print_help(sys.stderr)
        sys.exit(1)

    logging.info(f"Start training from epoch {last_epoch + 1}.")
    for epoch in range(last_epoch + 1, args.num_epochs):
        train(train_loader, net, criterion, optimizer, device=device, epoch=epoch)
        scheduler.step()

        if epoch % args.validation_epochs == 0 or epoch == args.num_epochs - 1:
            val_loss, val_regression_loss, val_classification_loss = test(val_loader, net, criterion, device)
            logging.info(
                f"Epoch: {epoch}, " +
                f"Validation Loss: {val_loss:.4f}, " +
                f"Validation Regression Loss {val_regression_loss:.4f}, " +
                f"Validation Classification Loss: {val_classification_loss:.4f}"
            )
            model_path = os.path.join(args.checkpoint_folder, f"{args.net}-Epoch-{epoch}-Loss-{val_loss}.pth")
            net.save(model_path)
            logging.info(f"Saved model {model_path}")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
