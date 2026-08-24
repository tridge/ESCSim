#include "am32sim.h"

#include <math.h>
#include <stdio.h>

int main(void)
{
    if (am32sim_init(NULL) != 0) {
        fprintf(stderr, "am32sim_init failed\n");
        return 1;
    }

    double voltage = 0.0;
    double current = 0.0;
    double temperature = 0.0;
    am32sim_get_sensors(&voltage, &current, &temperature);
    if (!isfinite(voltage) || voltage <= 0.0 || !isfinite(current) ||
        !isfinite(temperature)) {
        fprintf(stderr, "invalid initial sensors: %.3f %.3f %.3f\n",
                voltage, current, temperature);
        return 2;
    }

    am32sim_set_tim1(1999, 0, 0, 0, 20833, 500);
    am32sim_set_bridge(0, 0, 0);
    am32sim_advance(1000000, 0);

    double omega = 0.0;
    double theta = 0.0;
    double rpm = 0.0;
    am32sim_get_state(&omega, &theta, &rpm);
    if (!isfinite(omega) || !isfinite(theta) || !isfinite(rpm)) {
        fprintf(stderr, "invalid initial motor state\n");
        return 3;
    }
    return 0;
}
