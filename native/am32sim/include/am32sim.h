#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int am32sim_init(const char *config_path);
void am32sim_set_bridge(int a, int b, int c);
void am32sim_set_tim1(uint32_t arr, uint32_t ccr_a, uint32_t ccr_b,
                      uint32_t ccr_c, uint32_t tick_ps, uint32_t dead_ns);
void am32sim_set_comp_phase(int phase);
int am32sim_advance(uint64_t now_ns, int driven);
uint32_t am32sim_get_comp_toggles(void);
void am32sim_get_sensors(double *volts, double *amps, double *degrees);
void am32sim_get_currents(double currents[3]);
void am32sim_set_averaging(int on);
int am32sim_take_signals(double output[8]);
void am32sim_set_audio(int on);
int am32sim_take_audio(float output[], uint64_t times[], int max_samples);
int am32sim_reload_config(const char *path);
void am32sim_get_model(double *kv, int *poles);
void am32sim_set_theta(double theta);
void am32sim_set_stuck(double stuck);
double am32sim_get_stuck(void);
void am32sim_get_state(double *omega, double *theta, double *rpm);

#ifdef __cplusplus
}
#endif
