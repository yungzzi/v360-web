package org.v360web.bridge;

public final class PairingGateCheck {
    private static void require(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        PairingGate gate = new PairingGate();
        require(!gate.claim("000000", 0));
        String first = gate.issue(100);
        require(first.matches("[0-9]{6}"));
        require(gate.claim(first, 101));
        require(!gate.claim(first, 102));
        String expired = gate.issue(1000);
        require(!gate.claim(expired, 31000));
        String limited = gate.issue(40000);
        for (int i = 0; i < 5; i++) require(!gate.claim("invalid", 40001));
        require(!gate.claim(limited, 40002));
        String reset = gate.issue(50000);
        require(gate.claim(reset, 50001));
        System.out.println("Pairing gate: closed, format, one-use, expiry, attempt limit, reset passed");
    }
}
