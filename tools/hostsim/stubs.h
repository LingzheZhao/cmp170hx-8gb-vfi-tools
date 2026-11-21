/* Host simulation stubs for the glitch section of firmware/pwmvid.c (non-DEBUG).
   1 us simulated clock. PMBus writes take SIM_W_US (first write: SIM_FIRST_W_US).
   The alarm handler runs from inside any time advance when interrupts are on (models the IRQ).
   Controls (env): SIM_NACK="21#2,b9#*" (k-th write to reg fails, * = all), SIM_TAINT=30 (reg reads its
   fast value), SIM_VOUT=713 (READ_VOUT), SIM_SCL_STUCK_US=n (SCL reads low n us after release),
   SIM_NO_IRQ=1 (the alarm never interrupts). Every printf is logged as a PRINT event with its time. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
typedef unsigned int uint;
typedef uint64_t absolute_time_t;
typedef struct { int dummy; } i2c_inst_t;
static i2c_inst_t i2c0_inst, i2c1_inst;
#define i2c0 (&i2c0_inst)
#define i2c1 (&i2c1_inst)
#define GPIO_IN 0
#define GPIO_OUT 1
#define GPIO_FUNC_I2C 3
#define PICO_HIGHEST_IRQ_PRIORITY 0
#define __not_in_flash_func(f) f
typedef struct { uint32_t intr, inte; } sim_timer_t;
extern sim_timer_t sim_timer;
#define PICO_DEFAULT_TIMER_INSTANCE() (&sim_timer)
#define hw_set_bits(p, m) (*(p) |= (m))
extern FILE *ev;
extern uint64_t now;
extern int w_us, first_w_us, r_us, scl_stuck_us, no_irq;
extern uint64_t scl_released_at;
typedef void (*irq_handler_t)(void);
extern irq_handler_t alarm_isr;
extern bool alarm_armed, alarm_pending, irq_off;
extern uint64_t alarm_target;
static inline void sim_check_alarm(void) {
    if (alarm_armed && now >= alarm_target) { alarm_armed = false; alarm_pending = true; }
    if (alarm_pending && !irq_off && !no_irq && alarm_isr) { alarm_pending = false; fprintf(ev, "%llu IRQ alarm\n", (unsigned long long)now); alarm_isr(); }
}
static inline void sim_advance(uint64_t us) {
    for (uint64_t i = 0; i < us; i++) { now++; sim_check_alarm(); }
    if (now > 1000000 + 60000000ull) { fprintf(ev, "%llu SIM WATCHDOG\n", (unsigned long long)now); fflush(ev); abort(); }
}
static inline uint64_t time_us_64(void) { sim_check_alarm(); return now; }
static inline uint32_t time_us_32(void) { return (uint32_t)now; }
static inline void tight_loop_contents(void) { sim_advance(1); }
static inline void sleep_ms(uint32_t ms) { sim_advance((uint64_t)ms * 1000); }
static inline void sleep_us(uint64_t us) { sim_advance(us); }
static inline void busy_wait_us(uint64_t us) { sim_advance(us); }
static inline absolute_time_t from_us_since_boot(uint64_t t) { return t; }
static inline absolute_time_t make_timeout_time_us(uint64_t us) { return now + us; }
static inline bool time_reached(absolute_time_t t) { sim_advance(1); return now >= t; }
static uint8_t gpio_dir[64], gpio_out[64];
static inline void gpio_init(uint p) { gpio_dir[p] = 0; gpio_out[p] = 0; }
static inline void gpio_disable_pulls(uint p) { (void)p; }
static inline void gpio_set_dir(uint p, bool out) {
    if (gpio_dir[p] != out) {
        fprintf(ev, "%llu GPIO %u %s\n", (unsigned long long)now, p, out ? "OUT" : "IN");
        if (out && gpio_out[p]) fprintf(ev, "%llu GPIO %u DRIVE_HIGH (FORBIDDEN)\n", (unsigned long long)now, p);
        if (p == 1 && !out) scl_released_at = now;
    }
    gpio_dir[p] = out;
}
static inline void gpio_put(uint p, bool v) { gpio_out[p] = v; if (v && gpio_dir[p]) fprintf(ev, "%llu GPIO %u DRIVE_HIGH (FORBIDDEN)\n", (unsigned long long)now, p); }
static inline bool gpio_get(uint p) {
    if (gpio_dir[p]) return gpio_out[p];
    if (p == 1 && now < scl_released_at + (uint64_t)scl_stuck_us) return 0;
    return 1;
}
static inline void gpio_set_function(uint p, int f) { (void)p; (void)f; }
static inline uint i2c_init(i2c_inst_t *i, uint baud) { (void)i; return baud; }
static inline void i2c_deinit(i2c_inst_t *i) { (void)i; }
int sim_i2c_write(uint8_t a, const uint8_t *b, size_t n, bool nostop);
int sim_i2c_read(uint8_t a, uint8_t *d, size_t n);
#define i2c_write_timeout_us(i, a, b, n, nostop, t) sim_i2c_write(a, b, n, nostop)
#define i2c_read_timeout_us(i, a, d, n, nostop, t) sim_i2c_read(a, d, n)
static inline void irq_set_priority(uint irq, uint8_t p) { (void)irq; (void)p; }
static inline void irq_set_enabled(uint irq, bool e) { (void)irq; (void)e; }
static inline void irq_set_exclusive_handler(uint irq, irq_handler_t h) { (void)irq; alarm_isr = h; }
static inline void irq_clear(uint irq) { (void)irq; alarm_pending = false; }
static inline uint hardware_alarm_get_irq_num(uint a) { return a; }
static inline int hardware_alarm_claim_unused(bool req) { (void)req; return 0; }
static inline bool hardware_alarm_set_target(uint a, absolute_time_t t) {
    (void)a; if (now >= t) return true;
    alarm_target = t; alarm_armed = true; alarm_pending = false;
    fprintf(ev, "%llu ALARM armed for %llu\n", (unsigned long long)now, (unsigned long long)t); return false;
}
static inline void hardware_alarm_cancel(uint a) { (void)a; alarm_armed = false; }
static inline uint32_t save_and_disable_interrupts(void) { uint32_t s = irq_off; irq_off = true; return s; }
static inline void restore_interrupts(uint32_t s) { irq_off = s; sim_check_alarm(); }
int sim_printf(const char *fmt, ...);
#define printf(...) sim_printf(__VA_ARGS__)
