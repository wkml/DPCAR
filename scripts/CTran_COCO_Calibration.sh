#!/bin/bash

cd ..

OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 CUDA_VISIBLE_DEVICES=0 python CTran_calibration.py dataset=COCO model=CTran