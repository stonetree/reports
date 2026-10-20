#ifndef CONTROL_PLANE_PROBE_H
#define CONTROL_PLANE_PROBE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// 控制面阶段枚举
enum ProbeStage {
    STAGE_PREFIX_HASH    = 0, // 前缀哈希与检索阶段
    STAGE_ROUTING_DECIDE = 1, // 动态选路与准入门禁阶段
    STAGE_DESC_COMPILE   = 2, // 描述符编译与提交阶段
    STAGE_HBM_EVICT      = 3, // 显存换出决策阶段
    STAGE_PREFILL_EXEC   = 4, // 物理 Prefill 计算执行
    STAGE_DECODE_STEP    = 5  // 单步 Decode 计算执行
};

// 事件标记位
#define EVENT_FLAG_NONE          0x0000
#define EVENT_FLAG_INTENT_HIT    0x0001 // 命中意图句柄 (STATE_IN_FLIGHT)
#define EVENT_FLAG_FALSE_MISS    0x0002 // 遭遇伪未命中
#define EVENT_FLAG_PREEMPTION    0x0004 // 触发 HBM Preemption 抢占
#define EVENT_FLAG_FALLBACK_REC  0x0008 // 触发本地重算回退
#define EVENT_FLAG_STALE_ROUTE   0x0010 // 触发陈旧选路负收益

#pragma pack(push, 8)
// 64 字节对齐无锁事件记录体
typedef struct {
    uint64_t timestamp_cnt;  // ARM 硬件虚拟计数器 CNTVCT_EL0
    uint32_t request_id;     // 全局请求唯一 ID
    uint16_t stage_type;     // 阶段类型 ProbeStage
    uint16_t flags;          // 事件标记位
    float metric_val;        // 关联数值 (如当前 HBM 水位或计算裕量)
    uint32_t payload_bytes;  // 载荷大小
} TraceEventRecord;
#pragma pack(pop)

// API 导出
void init_probe_subsystem(void);
void inject_delay_ns(uint64_t target_delay_ns);
void set_stage_delay(int stage_id, double delay_ms);
double get_stage_delay(int stage_id);
void inject_stage_delay(int stage_id);

void record_probe_event(uint32_t req_id, uint16_t stage, uint16_t flags, float val, uint32_t bytes);
size_t get_ring_buffer_head(void);
int dump_ring_buffer(const char* filepath);
void reset_ring_buffer(void);

#ifdef __cplusplus
}
#endif

#endif // CONTROL_PLANE_PROBE_H
