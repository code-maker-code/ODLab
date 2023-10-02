# 8 GPUs
python -m torch.distributed.run --nproc_per_node=8 main.py --cuda -dist -d coco --root /data/datasets/COCO/ -m fcos_r50_1x --batch_size 16 --eval_epoch 2
