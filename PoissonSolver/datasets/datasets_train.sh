#!/usr/bin/bash
python3 rhs_random.py -c train.yml -nr 4 -nn 101
python3 rhs_random.py -c train.yml -nr 8 -nn 101
python3 rhs_random.py -c train.yml -nr 12 -nn 101
python3 rhs_random.py -c train.yml -nr 16 -nn 101
