# coding=utf-8
import os
import logging
import numpy as np
import mxnet as mx
from mxnet.gluon.data import DataLoader
from mxnet.gluon import Trainer
from mxnet import autograd, lr_scheduler as ls
from tensorboardX import SummaryWriter

from .utils import to_cpu, split_and_load
from .datasets.dataloader import ICDAR
from .model.loss import DiceLoss, DiceLoss_with_OHEM
from .model.net import PSENet


logger = logging.getLogger(__name__)


def train(
    data_dir,
    pretrain_model,
    optimizer,
    epochs=50,
    lr=0.001,
    wd=5e-4,
    momentum=0.9,
    batch_size=4,
    ctx=mx.cpu(),
    verbose_step=5,
    ckpt='ckpt',
):
    num_kernels = 3
    icdar_ds = ICDAR(root_dir=data_dir, num_kernels=num_kernels - 1)
    if not isinstance(ctx, (list, tuple)):
        ctx = [ctx]
    batch_size = batch_size * len(ctx)
    loader = DataLoader(icdar_ds, batch_size=batch_size, shuffle=True)
    net = PSENet(num_kernels=num_kernels, ctx=ctx, pretrained=True)
    # initial params
    net.initialize(mx.init.Xavier(), ctx=ctx)
    net.collect_params("extra_.*_weight|decoder_.*_weight").initialize(
        mx.init.Xavier(), ctx=ctx, force_reinit=True
    )
    net.collect_params("extra_.*_bias|decoder_.*_bias").initialize(
        mx.init.Zero(), ctx=ctx, force_reinit=True
    )
    # net.collect_params("!(resnet*)").setattr("lr_mult", 10)
    # net.collect_params("!(resnet*)").setattr('grad_req', 'null')
    net.load_parameters(pretrain_model, ctx=ctx, allow_missing=True, ignore_extra=True)

    # pse_loss = DiceLoss(lam=0.7, num_kernels=num_kernels)
    pse_loss = DiceLoss_with_OHEM(lam=0.7, num_kernels=num_kernels, debug=False)

    # lr_scheduler = ls.PolyScheduler(
    #     max_update=icdar_loader.length * epochs // batch_size, base_lr=lr
    # )
    max_update = len(icdar_ds) * epochs // batch_size
    lr_scheduler = ls.MultiFactorScheduler(
        base_lr=lr, step=[max_update // 3, max_update * 2 // 3], factor=0.1
    )

    optimizer_params = {
        'learning_rate': lr,
        'wd': wd,
        'momentum': momentum,
        'lr_scheduler': lr_scheduler,
    }
    if optimizer.lower() == 'adam':
        optimizer_params.pop('momentum')

    trainer = Trainer(
        net.collect_params(), optimizer=optimizer, optimizer_params=optimizer_params
    )
    summary_writer = SummaryWriter(ckpt)
    for e in range(epochs):
        cumulative_loss = 0

        num_batches = 0
        for i, item in enumerate(loader):
            item_ctxs = [split_and_load(field, ctx) for field in item]
            loss_list = []
            for im, gt_text, gt_kernels, training_masks, ori_img in zip(*item_ctxs):
                gt_text = gt_text[:, ::4, ::4]
                gt_kernels = gt_kernels[:, :, ::4, ::4]
                training_masks = training_masks[:, ::4, ::4]

                with autograd.record():
                    kernels_pred = net(im)  # 第0个是对complete text的预测
                    loss = pse_loss(gt_text, gt_kernels, kernels_pred, training_masks)
                    loss_list.append(loss)
            mean_loss = []
            for loss in loss_list:
                loss.backward()
                mean_loss.append(mx.nd.mean(to_cpu(loss)).asscalar())
            mean_loss = np.mean(mean_loss)
            trainer.step(batch_size)
            if i % verbose_step == 0:
                global_steps = icdar_ds.length * e + i * batch_size
                summary_writer.add_image(
                    'gt_text', to_cpu(gt_text[0:1, :, :]), global_steps
                )
                summary_writer.add_image(
                    'text_pred', to_cpu(kernels_pred[0:1, 0, :, :]), global_steps
                )
                summary_writer.add_image(
                    'gt_kernels[0]', to_cpu(gt_kernels[0:1, 0, :, :]), global_steps
                )
                summary_writer.add_image(
                    'kernels[0]_pred', to_cpu(kernels_pred[0:1, 1, :, :]), global_steps
                )
                summary_writer.add_scalar('loss', mean_loss, global_steps)
                summary_writer.add_scalar(
                    'c_loss',
                    mx.nd.mean(to_cpu(pse_loss.C_loss)).asscalar(),
                    global_steps,
                )
                summary_writer.add_scalar(
                    'kernel_loss',
                    mx.nd.mean(to_cpu(pse_loss.kernel_loss)).asscalar(),
                    global_steps,
                )
                summary_writer.add_scalar(
                    'pixel_accuracy', pse_loss.pixel_acc, global_steps
                )
            if i % 1 == 0:
                logger.info(
                    "step: {}, lr: {}, "
                    "loss: {}, score_loss: {}, kernel_loss: {}, pixel_acc: {}, kernel_acc: {}".format(
                        i * batch_size,
                        trainer.learning_rate,
                        mean_loss,
                        mx.nd.mean(to_cpu(pse_loss.C_loss)).asscalar(),
                        mx.nd.mean(to_cpu(pse_loss.kernel_loss)).asscalar(),
                        pse_loss.pixel_acc,
                        pse_loss.kernel_acc,
                    )
                )
            cumulative_loss += mean_loss
            num_batches += 1
        logger.info(
            "Epoch {}, mean loss: {}\n".format(e, cumulative_loss / num_batches)
        )
        net.save_parameters(os.path.join(ckpt, 'model_{}.param'.format(e)))
    summary_writer.close()
