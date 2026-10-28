/*
 * Debugger cockpit sample target (AHCC 6.0, Atari ST).
 *
 * The program prints the addresses of its own test variables and functions,
 * then waits for the host to write 0xC0DE to GO_FLAG.  That makes it a stable
 * target for breakpoints, stepping and memory watches.  After it is armed it
 * increments COUNTER, prints a few ticks, then deliberately executes an
 * illegal instruction so the cockpit has a real exception to triage.
 *
 * Write 1 to CRASH_FLAG to force the crash early.
 */

#include <stdio.h>

volatile unsigned short g_go = 0;
volatile unsigned short g_crash = 0;
volatile unsigned long g_counter = 0;
volatile unsigned short g_watchdog = 0;

static void spin(unsigned long count)
{
    volatile unsigned long i;
    for (i = 0; i < count; i++) {
        /* Keep the loop visible in memory and stop over-eager optimisation. */
        g_watchdog = (unsigned short)(g_watchdog + 1);
    }
}

void __asm__ cockpit_crash(void)
{
    illegal
}

void cockpit_tick(void)
{
    g_counter++;
    g_watchdog = (unsigned short)((g_counter * 7UL) ^ 0x5a5aUL);
    printf("TICK %lu counter=%lu watchdog=$%04x\n",
           (unsigned long)g_counter,
           (unsigned long)g_counter,
           (unsigned int)g_watchdog);
    spin(25000UL);
}

int main(int argc, char **argv)
{
    unsigned long i;

    (void)argc;
    (void)argv;

    printf("\nCOCKPIT SAMPLE READY\n");
    printf("GO_FLAG=$%08lx   (write c0de to arm)\n", (unsigned long)&g_go);
    printf("CRASH_FLAG=$%08lx (write 0001 to force crash)\n", (unsigned long)&g_crash);
    printf("COUNTER=$%08lx\n", (unsigned long)&g_counter);
    printf("TICK_FN=$%08lx\n", (unsigned long)cockpit_tick);
    printf("CRASH_FN=$%08lx\n", (unsigned long)cockpit_crash);
    printf("waiting for GO_FLAG...\n");

    while (g_go != 0xC0DE) {
        spin(20000UL);
    }

    printf("ARMED: running 12 ticks then deliberate exception\n");
    for (i = 0; i < 12UL; i++) {
        cockpit_tick();
        if (g_crash != 0) {
            printf("CRASH_FLAG set by host at tick %lu\n", (unsigned long)i);
            break;
        }
    }

    printf("deliberate illegal instruction now (PC follows in debugger)\n");
    cockpit_crash();
    printf("unreachable after the illegal instruction\n");
    return 0;
}
