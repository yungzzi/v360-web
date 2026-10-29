// Invoke the original exported C API without Android/JNI or ADB.
// Default is offline; INITIALIZE and CONNECT enable bounded extra probes.
// Private parameters arrive on stdin, never in source or command arguments.
.text
.global _start
.type _start, %function
_start:
.ifdef INITIALIZE
    mov x0, #0
    adrp x1, parameters
    add x1, x1, :lo12:parameters
    mov x2, #4095
    mov x8, #63
    svc #0
    cmp x0, #1
    b.lt input_error
    strb wzr, [x1, x0]
    mov x0, x1
    bl PPCS_Initialize
    mov w19, w0
    mov w20, #-1
.ifdef CONNECT
    mov w21, #-1
    mov w22, #-1
.endif
    cbnz w0, initialized
.ifdef CONNECT
    adrp x0, parameters
    add x0, x0, :lo12:parameters
    ldrb w1, [x0, #4094]
    add x3, x0, #2176
    add x0, x0, #2048
    mov w2, #0
    bl PPCS_ConnectByServer
    mov w22, w0
    tbnz w0, #31, no_session
    bl PPCS_ForceClose
    mov w21, w0
no_session:
.endif
    bl PPCS_DeInitialize
    mov w20, w0
initialized:
    mov w0, w19
.else
    bl PPCS_GetAPIVersion
.endif
    adrp x1, output
    add x1, x1, :lo12:output
    bl format_hex
.ifdef INITIALIZE
    mov w0, w20
    add x1, x1, #9
    bl format_hex
.ifdef CONNECT
    mov w0, w22
    add x1, x1, #9
    bl format_hex
    mov w0, w21
    add x1, x1, #9
    bl format_hex
    sub x1, x1, #27
    mov x2, #36
.else
    sub x1, x1, #9
    mov x2, #18
.endif
.else
    mov x2, #9
.endif
    mov x0, #1
    mov x8, #64
    svc #0
    mov x0, #0
    mov x8, #93
    svc #0
.ifdef INITIALIZE
input_error:
    mov x0, #2
    mov x8, #93
    svc #0
.endif
.size _start, .-_start
format_hex:
    adrp x3, digits
    add x3, x3, :lo12:digits
    mov w4, #7
1:
    and w5, w0, #15
    ldrb w5, [x3, w5, uxtw]
    strb w5, [x1, w4, uxtw]
    lsr w0, w0, #4
    subs w4, w4, #1
    b.ge 1b
    ret
.section .rodata
digits: .ascii "0123456789abcdef"
.data
output:
.ifdef INITIALIZE
.ifdef CONNECT
    .ascii "00000000 00000000 00000000 00000000\n"
.else
    .ascii "00000000 00000000\n"
.endif
.bss
.balign 16
parameters: .skip 4096
.else
    .ascii "00000000\n"
.endif
.section .note.GNU-stack,"",%progbits
