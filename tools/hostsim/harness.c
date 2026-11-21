#include <stdarg.h>
#include "stubs.h"
FILE *ev;
uint64_t now = 1000000;
int w_us = 47, first_w_us = -1, r_us = 60, scl_stuck_us = 0, no_irq = 0;
uint64_t scl_released_at;
irq_handler_t alarm_isr;
bool alarm_armed, alarm_pending, irq_off;
uint64_t alarm_target;
sim_timer_t sim_timer;
static uint8_t pointer_reg;
static int taint_reg = -1;
static uint16_t read_vout = 713;
static int nwrites[256], nwrites_total;
static char nack_spec[256];
static uint16_t regval[256];

static bool nack(uint8_t reg) {
    char key[16];
    snprintf(key, sizeof key, "%02x#*", reg);
    if (strstr(nack_spec, key)) return true;
    snprintf(key, sizeof key, "%02x#%d,", reg, nwrites[reg]);
    char spec[260]; snprintf(spec, sizeof spec, "%s,", nack_spec);
    return strstr(spec, key) != NULL;
}
static bool bus_low(void) { return !gpio_get(1); }
int sim_i2c_write(uint8_t a, const uint8_t *b, size_t n, bool nostop) {
    if (nostop) { pointer_reg = b[0]; sim_advance(r_us / 2); return (int)n; }
    nwrites[b[0]]++; nwrites_total++;
    fprintf(ev, "%llu W %02x reg=%02x", (unsigned long long)now, a, b[0]);
    for (size_t k = 1; k < n; k++) fprintf(ev, " %02x", b[k]);
    bool fail = nack(b[0]);
    fprintf(ev, "%s\n", fail ? " NACK" : "");
    sim_advance(nwrites_total == 1 && first_w_us >= 0 ? (uint64_t)first_w_us : (uint64_t)w_us);
    if (!fail && n == 3) regval[b[0]] = (uint16_t)(b[1] | b[2] << 8);
    if (!fail && n == 2) regval[b[0]] = b[1];
    return fail ? -1 : (int)n;
}
int sim_i2c_read(uint8_t a, uint8_t *d, size_t n) {
    sim_advance(r_us / 2);
    fprintf(ev, "%llu R %02x reg=%02x n=%zu\n", (unsigned long long)now, a, pointer_reg, n);
    uint16_t v = pointer_reg == 0x8B ? read_vout : regval[pointer_reg];
    if (pointer_reg == taint_reg) {
        static const struct { uint8_t r; uint16_t f; } F[] = { {0xBA,1},{0xB9,1},{0x30,0xCE04},{0x31,2},{0x32,0x0F81},{0x45,0x7218} };
        for (unsigned i = 0; i < 6; i++) if (F[i].r == pointer_reg) v = F[i].f;
    }
    for (size_t k = 0; k < n; k++) d[k] = (uint8_t)(v >> (8 * k));
    return (int)n;
}
int sim_printf(const char *fmt, ...) {
    fprintf(ev, "%llu PRINT\n", (unsigned long long)now);
    va_list ap; va_start(ap, fmt); int r = vprintf(fmt, ap); va_end(ap); return r;
}

#include "glitch_section.c"
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: harness <events-file> '<cmd>[;<cmd>...]'\n"); return 2; }
    ev = fopen(argv[1], "w");
    const char *e;
    if ((e = getenv("SIM_W_US"))) w_us = atoi(e);
    if ((e = getenv("SIM_FIRST_W_US"))) first_w_us = atoi(e);
    if ((e = getenv("SIM_SCL_STUCK_US"))) scl_stuck_us = atoi(e);
    if ((e = getenv("SIM_NO_IRQ"))) no_irq = atoi(e);
    if ((e = getenv("SIM_TAINT"))) taint_reg = (int)strtol(e, NULL, 16);
    if ((e = getenv("SIM_VOUT"))) read_vout = (uint16_t)atoi(e);
    if ((e = getenv("SIM_NACK"))) snprintf(nack_spec, sizeof nack_spec, "%s", e);
    /* a card controller: MATCH, stock profile, E1 PMBus mode */
    regval[0x7E] = 0x08; regval[0xE1] = 0xC2FA; regval[0x46] = 0x1229;
    regval[0xBA] = 0x000E; regval[0xB9] = 0x0039; regval[0x30] = 0xCE64; regval[0x31] = 0x1872;
    regval[0x32] = 0x7F81; regval[0x45] = 0x7018;
#ifdef HAVE_ALARM2
    rst_alarm_init();
#endif
    card_ctl_init();
    char all[512]; snprintf(all, sizeof all, "%s", argv[2]);
    for (char *cmd = strtok(all, ";"); cmd; cmd = strtok(NULL, ";")) {
        char line[96]; snprintf(line, sizeof line, "%s", cmd);
        fprintf(ev, "%llu CMD %s\n", (unsigned long long)now, line);
        if (line[0] == 'u') unlock_cmd();
        else if (line[0] == 'K') profile_cmd(false);
        else glitch_cmd(line);
        fflush(stdout);
    }
    fprintf(ev, "%llu END pex_dir=%u\n", (unsigned long long)now, gpio_dir[PEX_RST_PIN]);
    fclose(ev);
    return 0;
}
