package com.aion.chat;

import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Paint;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.TextView;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

public class LauncherActivity extends AppCompatActivity {

    private static final String PREFS       = "aion_prefs";
    private static final String KEY_URL     = "saved_url";
    private static final String KEY_AUTO    = "auto_connect";
    private static final String KEY_HOME    = "url_home";
    private static final String KEY_OUTDOOR = "url_outdoor";

    private static final String DEFAULT_HOME    = "http://192.168.x.x:8080/chat";
    private static final String DEFAULT_OUTDOOR = "http://192.168.x.x:8080/chat";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        if (prefs.getBoolean(KEY_AUTO, false)) {
            String savedUrl = prefs.getString(KEY_URL, getHomeUrl(prefs));
            launchWebView(savedUrl);
            return;
        }

        setContentView(R.layout.activity_launcher);

        TextView tvHome    = findViewById(R.id.tvHomeUrl);
        TextView tvOutdoor = findViewById(R.id.tvOutdoorUrl);
        Button   btnHome   = findViewById(R.id.btnHome);
        Button   btnOutdoor= findViewById(R.id.btnOutdoor);
        CheckBox cbRemember= findViewById(R.id.cbRemember);

        tvHome.setText(getHomeUrl(prefs));
        tvOutdoor.setText(getOutdoorUrl(prefs));
        tvHome.setPaintFlags(tvHome.getPaintFlags() | Paint.UNDERLINE_TEXT_FLAG);
        tvOutdoor.setPaintFlags(tvOutdoor.getPaintFlags() | Paint.UNDERLINE_TEXT_FLAG);

        tvHome.setOnClickListener(v -> showEditDialog(prefs, KEY_HOME, tvHome));
        tvOutdoor.setOnClickListener(v -> showEditDialog(prefs, KEY_OUTDOOR, tvOutdoor));

        btnHome.setOnClickListener(v -> {
            String url = getHomeUrl(prefs);
            saveIfNeeded(prefs, cbRemember.isChecked(), url);
            launchWebView(url);
        });

        btnOutdoor.setOnClickListener(v -> {
            String url = getOutdoorUrl(prefs);
            saveIfNeeded(prefs, cbRemember.isChecked(), url);
            launchWebView(url);
        });
    }

    private String getHomeUrl(SharedPreferences prefs) {
        return prefs.getString(KEY_HOME, DEFAULT_HOME);
    }

    private String getOutdoorUrl(SharedPreferences prefs) {
        return prefs.getString(KEY_OUTDOOR, DEFAULT_OUTDOOR);
    }

    private void showEditDialog(SharedPreferences prefs, String key, TextView display) {
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(display.getText());
        input.setSelectAllOnFocus(true);

        new AlertDialog.Builder(this)
            .setTitle("修改地址")
            .setView(input)
            .setPositiveButton("保存", (d, w) -> {
                String url = input.getText().toString().trim();
                if (!url.isEmpty()) {
                    prefs.edit().putString(key, url).apply();
                    display.setText(url);
                }
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private void saveIfNeeded(SharedPreferences prefs, boolean remember, String url) {
        SharedPreferences.Editor editor = prefs.edit();
        editor.putString(KEY_URL, url);
        editor.putBoolean(KEY_AUTO, remember);
        editor.apply();
    }

    private void launchWebView(String url) {
        // 启动前台推送服务
        startPushService(url);

        Intent intent = new Intent(this, WebViewActivity.class);
        intent.putExtra("url", url);
        startActivity(intent);
        finish();
    }

    private void startPushService(String url) {
        // 启动前台服务（权限请求移到 WebViewActivity，因为本 Activity 会立即 finish）
        Intent serviceIntent = new Intent(this, AionPushService.class);
        serviceIntent.putExtra("url", url);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }
}
