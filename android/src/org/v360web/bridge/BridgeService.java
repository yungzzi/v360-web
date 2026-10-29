package org.v360web.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.util.Base64;
import com.p2p.pppp_api.PPCS_APIs;
import java.io.BufferedWriter;
import java.io.InputStream;
import java.io.OutputStreamWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

public final class BridgeService extends Service {
    public static volatile String status = "Stopped";
    public static volatile boolean stopping = false;
    private static volatile boolean available = false;
    private static final PairingGate pairing = new PairingGate();
    private static final int MAX = 1048576;
    private final Set<Socket> clients = ConcurrentHashMap.newKeySet();
    private final Set<Integer> sessions = ConcurrentHashMap.newKeySet();
    private final ExecutorService workers = Executors.newFixedThreadPool(2);
    private final Object sdkLock = new Object();
    private volatile boolean running;
    private ServerSocket listener;
    private String token;
    private String initialization;
    private boolean initialized;
    private String startupFailure;

    @Override public void onCreate() {
        super.onCreate();
        NotificationManager manager = (NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        manager.createNotificationChannel(new NotificationChannel("bridge", "USB camera bridge",
                NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        startForeground(1, new Notification.Builder(this, "bridge")
                .setContentTitle("V360 USB bridge")
                .setContentText("Local USB bridge active; tap to stop")
                .setSmallIcon(android.R.drawable.ic_menu_camera).setContentIntent(open).build());
        token = getSharedPreferences("bridge", MODE_PRIVATE).getString("token", "");
        try {
            PPCS_APIs.PPCS_GetAPIVersion();
            listener = new ServerSocket(8766, 2, InetAddress.getByName("127.0.0.1"));
            running = true;
            available = true;
            status = "Listening on phone loopback:8766\nUse adb forward, then run bridge-check.";
            new Thread(new Runnable() {
                @Override public void run() { acceptClients(); }
            }, "v360-accept").start();
        } catch (Throwable error) {
            startupFailure = "Bridge could not start: " + error.getClass().getSimpleName()
                    + ". Check phone ABI and native SDK compatibility.";
            status = startupFailure;
            stopSelf();
        }
    }

    public static synchronized String beginPairing() {
        if (!available || stopping) return null;
        return pairing.issue(android.os.SystemClock.elapsedRealtime());
    }

    private static synchronized boolean claimPairing(String code) {
        return pairing.claim(code, android.os.SystemClock.elapsedRealtime());
    }

    private void acceptClients() {
        while (running) {
            try {
                final Socket socket = listener.accept();
                if (clients.size() >= 2) { socket.close(); continue; }
                clients.add(socket);
                workers.submit(new Runnable() {
                    @Override public void run() { serve(socket); }
                });
            } catch (Exception error) {
                if (running) status = "USB connection listener failed";
            }
        }
    }

    private String readLine(InputStream stream) throws Exception {
        java.io.ByteArrayOutputStream buffer = new java.io.ByteArrayOutputStream();
        for (int i = 0; i <= 2 * MAX; i++) {
            int value = stream.read();
            if (value == -1) return buffer.size() == 0 ? null : "";
            if (value == '\n') return buffer.toString("UTF-8");
            buffer.write(value);
        }
        throw new IllegalArgumentException("Frame too large");
    }

    private void serve(Socket socket) {
        Set<Integer> owned = new HashSet<Integer>();
        try {
            socket.setSoTimeout(45000);
            socket.setTcpNoDelay(true);
            BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(
                    socket.getOutputStream(), StandardCharsets.UTF_8));
            String line;
            while (running && (line = readLine(socket.getInputStream())) != null) {
                JSONObject answer;
                try {
                    JSONObject request = new JSONObject(line);
                    if (request.optInt("version") == 1 && request.optString("op").equals("pair")) {
                        answer = claimPairing(request.optString("code"))
                                ? new JSONObject().put("ok", true).put("token", token)
                                : failure("pairing_denied", -1);
                        writer.write(answer.toString() + "\n"); writer.flush(); break;
                    }
                    if (!MessageDigest.isEqual(token.getBytes(StandardCharsets.UTF_8),
                            request.optString("token").getBytes(StandardCharsets.UTF_8)) || token.length() < 32) {
                        answer = failure("authentication", -1);
                        writer.write(answer.toString() + "\n"); writer.flush(); break;
                    }
                    if (request.optInt("version") != 1) throw new IllegalArgumentException();
                    answer = dispatch(request, owned);
                } catch (IllegalArgumentException error) {
                    answer = failure("invalid_request", -1);
                } catch (Throwable error) {
                    answer = failure("bridge_operation", -1);
                }
                writer.write(answer.toString() + "\n");
                writer.flush();
            }
        } catch (Exception ignored) {
            // The connection owns its SDK handles, including on cable disconnection.
        } finally {
            for (Integer session : owned) release(session);
            clients.remove(socket);
            try { socket.close(); } catch (Exception ignored) { }
        }
    }

    private JSONObject dispatch(JSONObject request, Set<Integer> owned) throws Exception {
        String op = request.getString("op");
        JSONObject answer = new JSONObject().put("ok", true);
        if (op.equals("status")) {
            return answer.put("version", 1).put("sdk_api_version", PPCS_APIs.PPCS_GetAPIVersion())
                    .put("active_sessions", sessions.size());
        }
        if (op.equals("initialize")) {
            String value = request.getString("init");
            if (value.length() < 4 || value.length() > 65536) throw new IllegalArgumentException();
            synchronized (sdkLock) {
                if (initialized && !value.equals(initialization)) return failure("profile_busy", -1);
                if (!initialized) {
                    int ret = PPCS_APIs.PPCS_Initialize(value.getBytes(StandardCharsets.UTF_8));
                    if (ret != 0) return failure("sdk_initialize", ret);
                    initialization = value;
                    initialized = true;
                }
            }
            return answer.put("ret", 0);
        }
        if (!initialized) return failure("sdk_not_initialized", -1);
        if (op.equals("connect")) {
            String did = request.getString("did");
            int mode = request.getInt("mode");
            if (!owned.isEmpty() || did.length() < 4 || did.length() > 128 || did.indexOf(0) >= 0
                    || mode < 0 || mode > 255 || request.optInt("udp_port") != 0)
                throw new IllegalArgumentException();
            String server = request.optString("server_key");
            int handle = server.isEmpty() ? PPCS_APIs.PPCS_Connect(did, (byte)mode, 0)
                    : PPCS_APIs.PPCS_ConnectByServer(did, (byte)mode, 0, server);
            if (handle < 0) return failure("sdk_connect", handle);
            owned.add(handle);
            sessions.add(handle);
            return answer.put("session", handle);
        }
        int handle = request.getInt("session");
        if (!owned.contains(handle)) return failure("session_owner", -1);
        if (op.equals("close")) {
            owned.remove(handle);
            release(handle);
            return answer.put("ret", 0);
        }
        int channel = request.getInt("channel");
        if (channel < 0 || channel > 255) throw new IllegalArgumentException();
        if (op.equals("write")) {
            byte[] data = Base64.decode(request.getString("data"), Base64.NO_WRAP);
            if (data.length > MAX) throw new IllegalArgumentException();
            int ret = PPCS_APIs.PPCS_Write(handle, (byte)channel, data, data.length);
            return ret < 0 ? failure("sdk_write", ret) : answer.put("ret", ret);
        }
        if (op.equals("read")) {
            int length = request.getInt("size");
            int timeout = request.getInt("timeout_ms");
            if (length < 1 || length > MAX || timeout < 1 || timeout > 1000)
                throw new IllegalArgumentException();
            byte[] data = new byte[length];
            int[] actual = new int[]{length};
            int ret = PPCS_APIs.PPCS_Read(handle, (byte)channel, data, actual, timeout);
            if (actual[0] < 0 || actual[0] > length) return failure("sdk_read_size", ret);
            return answer.put("ret", ret).put("data", Base64.encodeToString(data, 0, actual[0], Base64.NO_WRAP));
        }
        throw new IllegalArgumentException();
    }

    private JSONObject failure(String code, int ret) {
        JSONObject result = new JSONObject();
        try { result.put("ok", false).put("error", code).put("ret", ret); }
        catch (Exception ignored) { }
        return result;
    }

    private void release(int handle) {
        if (sessions.remove(handle)) {
            try { PPCS_APIs.PPCS_ForceClose(handle); } catch (Throwable ignored) { }
        }
    }

    @Override public void onDestroy() {
        running = false;
        available = false;
        stopping = true;
        status = startupFailure != null ? startupFailure : "Stopping and releasing native connections";
        try { if (listener != null) listener.close(); } catch (Exception ignored) { }
        try { PPCS_APIs.PPCS_Connect_Break(); } catch (Throwable ignored) { }
        for (Integer session : sessions) release(session);
        for (Socket socket : clients) try { socket.close(); } catch (Exception ignored) { }
        workers.shutdown();
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    // Retain ownership until blocked native calls really finish.
                    while (!workers.awaitTermination(1, TimeUnit.SECONDS)) { }
                    if (initialized) PPCS_APIs.PPCS_DeInitialize();
                    status = startupFailure != null ? startupFailure : "Stopped";
                } catch (Throwable error) { status = "Native cleanup could not be confirmed"; }
                finally { stopping = false; }
            }
        }, "v360-cleanup").start();
        super.onDestroy();
    }

    @Override public int onStartCommand(Intent intent, int flags, int id) { return START_NOT_STICKY; }
    @Override public IBinder onBind(Intent intent) { return null; }
}
