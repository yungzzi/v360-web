package org.v360web.bridge;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.util.Base64;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.security.SecureRandom;

public final class MainActivity extends Activity {
    private final Handler handler = new Handler();
    private TextView status;
    private String token;
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            status.setText(BridgeService.status + "\n\nABI: " + android.text.TextUtils.join(", ", Build.SUPPORTED_ABIS));
            handler.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        token = getSharedPreferences("bridge", MODE_PRIVATE).getString("token", null);
        if (token == null) {
            byte[] bytes = new byte[32];
            new SecureRandom().nextBytes(bytes);
            token = Base64.encodeToString(bytes, Base64.NO_WRAP | Base64.URL_SAFE);
            getSharedPreferences("bridge", MODE_PRIVATE).edit().putString("token", token).apply();
        }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(28, 28, 28, 28);
        TextView title = new TextView(this);
        title.setText("V360 USB Bridge\n\nConnect this phone to your computer. This helper listens only on phone loopback. It does not configure the camera.\n");
        layout.addView(title);
        status = new TextView(this);
        layout.addView(status);
        Button start = new Button(this);
        start.setText("Start bridge");
        start.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (BridgeService.stopping) return;
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
                }
                startForegroundService(new Intent(MainActivity.this, BridgeService.class));
            }
        });
        layout.addView(start);
        Button pair = new Button(this);
        pair.setText("Pair this computer (30 seconds)");
        pair.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                String code = BridgeService.beginPairing();
                if (code == null) { status.setText("Start the bridge first"); return; }
                new android.app.AlertDialog.Builder(MainActivity.this)
                        .setTitle("USB pairing code")
                        .setMessage(code + "\n\nEnter this code in v360 pair-bridge within 30 seconds.")
                        .setPositiveButton("OK", null).show();
            }
        });
        layout.addView(pair);
        Button copy = new Button(this);
        copy.setText("Copy private bridge token");
        copy.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                ClipboardManager clipboard = (ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
                clipboard.setPrimaryClip(ClipData.newPlainText("V360 bridge token", token));
            }
        });
        layout.addView(copy);
        Button stop = new Button(this);
        stop.setText("Stop and release connections");
        stop.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                stopService(new Intent(MainActivity.this, BridgeService.class));
            }
        });
        layout.addView(stop);
        setContentView(layout);
    }

    @Override public void onResume() { super.onResume(); handler.post(refresh); }
    @Override public void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
}
