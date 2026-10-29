package org.v360web.bridge;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Locale;

/** A user-opened, expiring, one-use USB pairing window. No Android dependencies. */
final class PairingGate {
    private final SecureRandom random = new SecureRandom();
    private String code;
    private long deadline;
    private int attempts;

    synchronized String issue(long now) {
        code = String.format(Locale.US, "%06d", random.nextInt(1000000));
        deadline = now + 30000;
        attempts = 0;
        return code;
    }

    synchronized boolean claim(String candidate, long now) {
        if (code == null || now >= deadline || ++attempts > 5) return false;
        if (!MessageDigest.isEqual(code.getBytes(StandardCharsets.UTF_8),
                candidate.getBytes(StandardCharsets.UTF_8))) return false;
        code = null;
        deadline = 0;
        return true;
    }
}
