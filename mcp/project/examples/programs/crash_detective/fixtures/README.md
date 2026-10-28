# Fixtures

`bus_error_loop.prg` is a deliberately failing Atari ST program used to
demonstrate the crash detective's PRG path.

It is based on the harmless `tests/autostart/4s_sleep.prg` test program from
this repository; the first text instructions were replaced with:

```
lea     $ffff8240.l,a0   ; ST palette register block
moveq   #15,d0
loop:   clr.w   (a0)+    ; read-modify-write -> bus error on the read
        dbf     d0,loop
        bra.s   *
```

The rest of the original text segment is NOP padding. The header/data/bss sizes
are preserved so TOS accepts the file. Run it with **Break on guest
exceptions** enabled to stop in the debugger, or watch the Hatari log for
`Bus Error reading at address ...`.

No emulator or host files are modified by the fixture.
