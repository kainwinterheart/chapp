#!/usr/bin/env python3

import sys
import time


def main():
    spec, text = sys.stdin.read().split("\n", maxsplit=1)
    cnt, flag = map(int, spec.split(" "))
    for i in range(cnt):
        if (i % 2) == flag:
            sys.stderr.write(f"> {i}\n")
            time.sleep(1)
    print(text)

if __name__ == "__main__":
    main()
