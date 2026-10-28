/*
 * tt_sample.c - tiny Atari ST target for the time-travel debugger example.
 *
 * The program paints the 320x200 low-resolution framebuffer every VBL and
 * updates a small, fixed RAM window at 0x00070000.  Both the screen and the
 * memory marker change over time, so periodic checkpoints are visibly and
 * byte-wise different.
 *
 * Build with the MCP AHCC tool (see ../sample.py) or:
 *   scripts/ahcc-cli.py tt_sample.c -o tt_sample.prg
 */

#include <tos.h>

#define MARKER      ((volatile unsigned long *)0x00070000L)
#define WORDS_PER_ROW 80u
#define ROWS 200u

#define MARKER_MAGIC 0x54544D31UL /* "TTM1" */
#define MARKER_TAIL  0xDEADBEEFUL

static void paint(unsigned short *screen, unsigned long frame)
{
	unsigned int y;
	unsigned int x;
	unsigned int phase = (unsigned int)(frame & 15UL);

	for (y = 0; y < ROWS; y++) {
		unsigned int band = ((y >> 4) + phase) & 15u;
		for (x = 0; x < WORDS_PER_ROW; x++) {
			unsigned int column = ((x >> 2) + (phase >> 1)) & 15u;
			unsigned int grain = ((x ^ (y >> 2)) + (unsigned int)frame) & 15u;
			unsigned int word = (band << 12) | (column << 8)
				| (grain << 4) | ((x + y + phase) & 15u);
			screen[y * WORDS_PER_ROW + x] = (unsigned short)word;
		}
	}
}

int main(void)
{
	unsigned long frame = 0;
	unsigned short *screen = (unsigned short *)Physbase();

	MARKER[0] = MARKER_MAGIC;
	MARKER[1] = 0;
	MARKER[2] = 0;
	MARKER[3] = MARKER_TAIL;

	for (;;) {
		unsigned long now;

		Vsync();
		frame++;
		now = frame;

		MARKER[0] = MARKER_MAGIC;
		MARKER[1] = frame;
		MARKER[2] = (frame * 0x9E3779B1UL) ^ now;
		MARKER[3] = MARKER_TAIL;

		paint(screen, frame);
	}
}
