// Private stdio worker. Request: op,session,channel,timeout,size,body_len (6 u32).
// Response: magic 0x56575031, signed SDK return, body_len; then optional bytes.
// One owned session per process. No credentials in arguments or logs.
.text
.global _start
_start:
    // Keep an exclusive protocol FD; redirect SDK stdout messages to stderr.
    mov x0, #1
    mov x8, #23 // dup
    svc #0
    mov x24, x0
    mov x0, #2
    mov x1, #1
    mov x2, #0
    mov x8, #24 // dup3
    svc #0
    adrp x19, request
    add x19, x19, :lo12:request
    adrp x20, buffer
    add x20, x20, :lo12:buffer
    mov w21, #-1 // owner
    mov w22, #0 // initialized
next:
    mov x0, #0
    mov x1, x19
    mov x2, #24
    bl read_all
    cbnz w0, cleanup
    ldr w0, [x19, #20]
    cmp w0, #0x100000
    b.hi bad_exit
    cbz w0, dispatch
    mov x2, x0
    mov x0, #0
    mov x1, x20
    bl read_all
    cbnz w0, cleanup
dispatch:
    mov w25, #0
    ldr w0, [x19]
    cmp w0, #6
    b.eq shutdown
    cbz w0, status
    cmp w0, #1
    b.eq initialize
    cbz w22, not_initialized
    cmp w0, #2
    b.eq connect
    ldr w1, [x19, #4]
    cmp w1, w21
    b.ne wrong_owner
    tbnz w21, #31, wrong_owner
    cmp w0, #3
    b.eq close_session
    ldr w1, [x19, #8]
    cmp w1, #255
    b.hi bad_exit
    cmp w0, #4
    b.eq write_data
    cmp w0, #5
    b.eq read_data
    b bad_exit
status:
    bl PPCS_GetAPIVersion
    b reply
initialize:
    cbnz w22, bad_exit
    ldr w1, [x19, #20]
    cmp w1, #4
    b.lo bad_exit
    mov w2, #65535
    cmp w1, w2
    b.hi bad_exit
    strb wzr, [x20, x1]
    mov x0, x20
    bl PPCS_Initialize
    cmp w0, #0
    cset w22, eq
    b reply
connect:
    tbz w21, #31, wrong_owner
    ldr w1, [x19, #20]
    cmp w1, #1152
    b.ne bad_exit
    ldr w1, [x19, #8]
    cmp w1, #255
    b.hi bad_exit
    strb wzr, [x20, #127]
    strb wzr, [x20, #1151]
    mov x0, x20
    mov w2, #0
    add x3, x20, #128
    ldrb w4, [x3]
    cbz w4, default_connect
    bl PPCS_ConnectByServer
    b connected
default_connect:
    bl PPCS_Connect
connected:
    tbnz w0, #31, reply
    mov w21, w0
    b reply
close_session:
    mov w0, w21
    bl PPCS_ForceClose
    cbnz w0, reply
    mov w21, #-1
    b reply
write_data:
    mov w0, w21
    ldr w1, [x19, #8]
    mov x2, x20
    ldr w3, [x19, #20]
    bl PPCS_Write
    b reply
read_data:
    ldr w2, [x19, #16]
    cbz w2, bad_exit
    cmp w2, #0x100000
    b.hi bad_exit
    ldr w4, [x19, #12]
    cbz w4, bad_exit
    cmp w4, #1000
    b.hi bad_exit
    mov x0, x20
    mov w1, #0
    bl memset
    ldr w2, [x19, #16]
    adrp x3, actual
    add x3, x3, :lo12:actual
    str w2, [x3]
    mov w0, w21
    ldr w1, [x19, #8]
    mov x2, x20
    ldr w4, [x19, #12]
    bl PPCS_Read
    mov w23, w0
    cbz w0, read_length
    cmn w0, #3
    b.ne reply
read_length:
    adrp x1, actual
    add x1, x1, :lo12:actual
    ldr w25, [x1]
    ldr w2, [x19, #16]
    cmp w25, w2
    b.hi bad_exit
    mov w0, w23
    b reply
not_initialized:
    mov w0, #-1002
    b reply
wrong_owner:
    mov w0, #-1001
    b reply
bad_exit:
    mov w0, #-1000
    mov w25, #0
    bl send_reply
    b cleanup
reply:
    bl send_reply
    cbnz w0, cleanup
    b next
shutdown:
    bl release_all
    // release_all stores its two return codes in the body buffer.
    mov w0, #0
    mov w25, #8
    bl send_reply
    b exit
cleanup:
    bl release_all
exit:
    mov x0, #0
    mov x8, #94 // exit_group: terminate the owned process and SDK threads
    svc #0
release_all:
    stp x29, x30, [sp, #-16]!
    mov w23, #0
    tbnz w21, #31, released
    mov w0, w21
    bl PPCS_ForceClose
    mov w23, w0
    mov w21, #-1
released:
    mov w0, #0
    cbz w22, deinitialized
    bl PPCS_DeInitialize
    mov w22, #0
deinitialized:
    str w23, [x20]
    str w0, [x20, #4]
    ldp x29, x30, [sp], #16
    ret
send_reply:
    stp x29, x30, [sp, #-16]!
    adrp x1, response
    add x1, x1, :lo12:response
    str w0, [x1, #4]
    str w25, [x1, #8]
    mov w0, #0x5031
    movk w0, #0x5657, lsl #16
    str w0, [x1]
    mov x0, x24
    mov x2, #12
    bl write_all
    cbnz w0, sent
    cbz w25, sent
    mov x0, x24
    mov x1, x20
    mov x2, x25
    bl write_all
sent:
    ldp x29, x30, [sp], #16
    ret
read_all:
    mov x11, x0
    mov x10, x1
    mov x9, x2
read_loop:
    mov x0, x11
    mov x1, x10
    mov x2, x9
    mov x8, #63
    svc #0
    cmp x0, #0
    b.le io_error
    add x10, x10, x0
    subs x9, x9, x0
    b.ne read_loop
    mov w0, #0
    ret
write_all:
    mov x11, x0
    mov x10, x1
    mov x9, x2
write_loop:
    mov x0, x11
    mov x1, x10
    mov x2, x9
    mov x8, #64
    svc #0
    cmp x0, #0
    b.le io_error
    add x10, x10, x0
    subs x9, x9, x0
    b.ne write_loop
    mov w0, #0
    ret
io_error:
    mov w0, #-1
    ret
.bss
.balign 16
request: .skip 24
response: .skip 12
actual: .skip 4
buffer: .skip 1048577
.section .note.GNU-stack,"",%progbits
