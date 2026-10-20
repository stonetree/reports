/*
 * ballast_alloc.c - 物理压舱石大页预锁定工具
 * 目的：在非计算绑定的 NUMA 节点上一次性预先分配并锁定指定大小的 1GB 巨页，
 *       构造跨 Die 物理内存碎片与 SMMUv3 压力，同时严格杜绝运行时动态内存申请引发的内核压缩抖动。
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/mman.h>
#include <signal.h>
#include <errno.h>

static volatile int g_keep_running = 1;

void handle_signal(int sig) {
    (void)sig;
    g_keep_running = 0;
}

int main(int argc, char* argv[]) {
    size_t size_gb = 450;
    if (argc > 1) {
        if (strcmp(argv[1], "--size-gb") == 0 && argc > 2) {
            size_gb = (size_t)atoll(argv[2]);
        } else if (strncmp(argv[1], "--size=", 7) == 0) {
            char* endptr = NULL;
            size_gb = (size_t)strtoull(argv[1] + 7, &endptr, 10);
        }
    }

    size_t total_bytes = size_gb * 1024ULL * 1024ULL * 1024ULL;
    printf("[Ballast Allocator] 准备分配并锁定 %zu GB 物理内存 (HugeTLB)...\n", size_gb);

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    // 尝试使用 1GB HugeTLB 分配
    int mmap_flags = MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB | MAP_POPULATE | MAP_LOCKED;
#if defined(MAP_HUGE_1GB)
    mmap_flags |= MAP_HUGE_1GB;
#endif

    void* ptr = mmap(NULL, total_bytes, PROT_READ | PROT_WRITE, mmap_flags, -1, 0);
    if (ptr == MAP_FAILED) {
        fprintf(stderr, "[Ballast Allocator] 1GB 巨页映射失败 (errno=%d: %s)。尝试常规匿名内存预锁...\n",
                errno, strerror(errno));
        mmap_flags = MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE | MAP_LOCKED;
        ptr = mmap(NULL, total_bytes, PROT_READ | PROT_WRITE, mmap_flags, -1, 0);
        if (ptr == MAP_FAILED) {
            fprintf(stderr, "[Ballast Allocator] 致命错误：常规内存映射亦失败 (errno=%d: %s)！\n",
                    errno, strerror(errno));
            return 1;
        }
    }

    printf("[Ballast Allocator] 内存映射成功: 地址区间 [%p, %p)\n", ptr, (char*)ptr + total_bytes);
    printf("[Ballast Allocator] 正在初始化触摸物理页框 (Stride: 2MB)... \n");

    // 步长 2MB 写入特征值，迫使所有物理页框完全提交至页表
    volatile char* c_ptr = (volatile char*)ptr;
    size_t stride = 2 * 1024 * 1024;
    for (size_t offset = 0; offset < total_bytes; offset += stride) {
        c_ptr[offset] = (char)(offset & 0xFF);
    }

    printf("[Ballast Allocator] 物理页框已全部就绪并锁定！进程 PID: %d\n", getpid());
    printf("[Ballast Allocator] 进入常驻挂起状态 (接收 SIGINT / SIGTERM 释放退出)...\n");
    fflush(stdout);

    while (g_keep_running) {
        sleep(1);
    }

    printf("[Ballast Allocator] 收到退出信号，开始解除内存锁定并释放物理页...\n");
    munmap(ptr, total_bytes);
    printf("[Ballast Allocator] 物理压舱石已安全释放。\n");
    return 0;
}
