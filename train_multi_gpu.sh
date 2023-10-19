# 8 GPUs
python -m torch.distributed.run --nproc_per_node=2 --master_port 1663 main.py --cuda -dist -d coco --root /data/datasets/COCO/ -m yolofv2_r18_c5_1x --batch_size 16 --eval_epoch 2
