#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: load-resnet.py
# Author: Eric Yujia Huang yujiah1@andrew.cmu.edu
#         Yuxin Wu <ppwwyyxx@gmail.com>
#

import cv2
import tensorflow as tf
import argparse
import os, re
import numpy as np
import six
from six.moves import zip
from tensorflow.contrib.layers import variance_scaling_initializer

from tensorpack import *
from tensorpack.utils import logger
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *
from tensorpack.dataflow.dataset import ILSVRCMeta

"""
Usage:
    python -m tensorpack.utils.loadcaffe PATH/TO/CAFFE/{ResNet-101-deploy.prototxt,ResNet-101-model.caffemodel} ResNet101.npy
    ./load-resnet.py --load ResNet-101.npy --input cat.png --depth 101
"""
MODEL_DEPTH = None

class Model(ModelDesc):
    def _get_input_vars(self):
        return [InputVar(tf.float32, [None, 224, 224, 3], 'input')]

    def _build_graph(self, input_vars):
        image = input_vars[0]

        def shortcut(l, n_in, n_out, stride):
            if n_in != n_out:
                l = Conv2D('convshortcut', l, n_out, 1, stride=stride)
                return BatchNorm('bnshortcut', l)
            else:
                return l

        def bottleneck(l, ch_out, stride, preact):
            ch_in = l.get_shape().as_list()[-1]
            input = l
            if preact == 'both_preact':
                l = tf.nn.relu(l, name='preact-relu')
                input = l
            l = Conv2D('conv1', l, ch_out, 1)
            l = BatchNorm('bn1', l)
            l = tf.nn.relu(l)
            l = Conv2D('conv2', l, ch_out, 3, stride=stride)
            l = BatchNorm('bn2', l)
            l = tf.nn.relu(l)
            l = Conv2D('conv3', l, ch_out * 4, 1)
            l = BatchNorm('bn3', l)  # put bn at the bottom
            return l + shortcut(input, ch_in, ch_out * 4, stride)

        def layer(l, layername, features, count, stride, first=False):
            with tf.variable_scope(layername):
                with tf.variable_scope('block0'):
                    l = bottleneck(l, features, stride,
                            'no_preact' if first else 'both_preact')
                for i in range(1, count):
                    with tf.variable_scope('block{}'.format(i)):
                        l = bottleneck(l, features, 1, 'both_preact')
                return l

        cfg = {
            50: ([3,4,6,3]),
            101: ([3,4,23,3]),
            152: ([3,8,36,3])
        }
        defs = cfg[MODEL_DEPTH]
        with argscope(Conv2D, nl=tf.identity, use_bias=False,
                W_init=variance_scaling_initializer(mode='FAN_OUT')):
            fc1000 = (LinearWrap(image)
                .Conv2D('conv0', 64, 7, stride=2, nl=BNReLU)
                .MaxPooling('pool0', shape=3, stride=2, padding='SAME')
                .apply(layer, 'group0', 64, defs[0], 1, first=True)
                .apply(layer, 'group1', 128, defs[1], 2)
                .apply(layer, 'group2', 256, defs[2], 2)
                .apply(layer, 'group3', 512, defs[3], 2)
                .tf.nn.relu()
                .GlobalAvgPooling('gap')
                .FullyConnected('fc1000', 1000, nl=tf.identity)())
            prob = tf.nn.softmax(fc1000, name='prob_output')

def run_test(params, input):
    image_mean = np.array([0.485, 0.456, 0.406], dtype='float32')
    pred_config = PredictConfig(
        model=Model(),
        input_var_names=['input'],
        session_init=ParamRestore(params),
        output_var_names=['prob_output']
    )
    predict_func = get_predict_func(pred_config)

    im = cv2.imread(input)
    im = cv2.resize(im, (224,224)) - image_mean * 255
    im = np.reshape( im, (1, 224, 224, 3)).astype('float32')
    prob = predict_func([im])[0]

    ret = prob[0].argsort()[-10:][::-1]
    print(ret)
    meta = ILSVRCMeta().get_synset_words_1000()
    print([meta[k] for k in ret])

def name_conversion(caffe_layer_name):
    # beginning & end mapping
    NAME_MAP = {'bn_conv1/beta': 'conv0/bn/beta',
            'bn_conv1/gamma': 'conv0/bn/gamma',
            'bn_conv1/mean/EMA': 'conv0/bn/mean/EMA',
            'bn_conv1/variance/EMA': 'conv0/bn/variance/EMA',
            'conv1/W': 'conv0/W',
            'conv1/b': 'conv0/b',
            'fc1000/W': 'fc1000/W',
            'fc1000/b': 'fc1000/b'}
    if caffe_layer_name in NAME_MAP:
        return NAME_MAP[caffe_layer_name]

    s = re.search('([a-z]+)([0-9]+)([a-z]+)_', caffe_layer_name)
    if s is None:
        s = re.search('([a-z]+)([0-9]+)([a-z]+)([0-9]+)_', caffe_layer_name)
        layer_block_part1 = s.group(3)
        layer_block_part2 = s.group(4)
        assert layer_block_part1 in ['a', 'b']
        layer_block = 0 if layer_block_part1 == 'a' else int(layer_block_part2)
    else:
        layer_block = ord(s.group(3)) - ord('a')
    layer_type = s.group(1)
    layer_group = s.group(2)

    layer_branch = int(re.search('_branch([0-9])', caffe_layer_name).group(1))
    assert layer_branch in [1, 2]
    if layer_branch == 2:
        layer_id = re.search('_branch[0-9]([a-z])/', caffe_layer_name).group(1)
        layer_id = ord(layer_id) - ord('a') + 1

    TYPE_DICT = {'res':'conv', 'bn':'bn'}

    tf_name = caffe_layer_name[caffe_layer_name.index('/'):]
    layer_type = TYPE_DICT[layer_type] + \
        (str(layer_id) if layer_branch == 2 else 'shortcut')
    tf_name = 'group{}/block{}/{}'.format(
            int(layer_group) - 2, layer_block, layer_type) + tf_name
    return tf_name

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.') # nargs='*' in multi mode
    parser.add_argument('--load',
                        help='.npy model file generated by tensorpack.utils.loadcaffe',
                        required=True)
    parser.add_argument('--input', help='an input image', required=True)
    parser.add_argument('--depth', help='resnet depth', required=True, type=int, choices=[50, 101, 152])

    args = parser.parse_args()
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    # run resNet with given model (in npy format)
    MODEL_DEPTH = args.depth

    param = np.load(args.load, encoding='latin1').item()
    resnet_param = {}
    for k, v in six.iteritems(param):
        try:
            newname = name_conversion(k)
        except:
            logger.error("Exception when processing caffe layer {}".format(k))
            raise
        logger.info("Name Transform: " + k + ' --> ' + newname)
        resnet_param[newname] = v

    run_test(resnet_param, args.input)
