package com.aion.chat;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import android.media.AudioAttributes;
import android.media.MediaPlayer;

import android.Manifest;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Bundle;
import androidx.core.content.ContextCompat;
import okhttp3.MediaType;
import okhttp3.RequestBody;

import android.app.usage.UsageStats;
import android.app.usage.UsageStatsManager;
import android.app.usage.UsageEvents;
import android.provider.Settings;

import android.content.BroadcastReceiver;
import android.content.IntentFilter;

/**
 * 前台服务 — OkHttp WebSocket 长连接
 * 针对 vivo/OPPO 等 ROM 做了适配：
 * 1. Thread.sleep 心跳（不依赖 Handler/Looper）
 * 2. ConnectivityManager.NetworkCallback 监听网络变化
 * 3. synchronized connectWebSocket 防并发竞争
 * 4. onFailure 不阻塞 OkHttp 回调线程
 * 5. fullScreenIntent 闹铃通知（锁屏也能亮屏弹出）
 */
public class AionPushService extends Service {

    private static final String TAG = "AionPush";

    private static final String CH_KEEPALIVE = "aion_keepalive";
    private static final String CH_MESSAGE   = "aion_message";
    private static final String CH_ALARM     = "aion_alarm";

    private static final int NOTIF_FOREGROUND = 1;
    private static final int NOTIF_MSG_BASE   = 1000;

    private static final long HEARTBEAT_MS  = 45_000;  // 45s 心跳（省电）
    private static final long HEALTH_TIMEOUT = 120_000; // 120s 无消息 → 重连

    private OkHttpClient client;
    private volatile WebSocket webSocket;
    private volatile String serverUrl;
    private int notifCounter = 0;

    private final AtomicInteger wsGeneration = new AtomicInteger(0);
    private final AtomicBoolean wsConnected = new AtomicBoolean(false);

    private volatile int reconnectDelay = 3000;
    private static final int MAX_RECONNECT_DELAY = 30000;
    private volatile boolean shouldRun = true;
    private volatile boolean isForegroundActive = false;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private Thread heartbeatThread;
    private MediaPlayer mediaPlayer;

    private volatile int msgReceived = 0;
    private volatile long lastMessageTime = 0;

    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;

    // ── 定位上报 ──
    private static final long LOCATION_INTERVAL = 10 * 60_000;          // 统一 10 分钟（服务端做智能过滤，非每次都调 API）
    private static final long LOCATION_INTERVAL_DISABLED = 10 * 60_000; // 功能未启用/静默时段时低频轮询开关状态
    private Thread locationThread;
    private volatile long locationInterval = LOCATION_INTERVAL;
    private LocationManager locationManager;
    private volatile Location lastKnownLocation;
    private volatile boolean locationEnabled = false;  // 服务端定位开关状态

    // ── 活动上报 ──
    private static final long ACTIVITY_INTERVAL = 60_000;  // 60秒检测一次前台应用
    private static final long ACTIVITY_RE_REPORT_MS = 5 * 60_000;  // 同一App超过5分钟重新上报
    private Thread activityThread;
    private volatile String lastReportedApp = "";
    private volatile long lastReportedTime = 0;
    private volatile boolean screenOn = true;
    private BroadcastReceiver screenReceiver;

    // ══════════════════════════════════════════════════════════
    //  生命周期
    // ══════════════════════════════════════════════════════════

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "=== onCreate ===");
        createNotificationChannels();

        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AionChat:Push");
            wakeLock.acquire();
            Log.i(TAG, "WakeLock acquired");
        }

        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "AionChat:Wifi");
            wifiLock.acquire();
            Log.i(TAG, "WifiLock acquired");
        }

        client = new OkHttpClient.Builder()
                .pingInterval(30, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.SECONDS)
                .connectTimeout(10, TimeUnit.SECONDS)
                .build();

        registerNetworkCallback();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null) {
            String action = intent.getStringExtra("action");
            if ("set_foreground".equals(action)) {
                isForegroundActive = intent.getBooleanExtra("active", false);
                if (isForegroundActive) stopMusic(); // WebView 接管，停止原生播放
                Log.d(TAG, "foreground=" + isForegroundActive);
                return START_STICKY;
            }

            String url = intent.getStringExtra("url");
            if (url != null) {
                String ws = url.replace("http://", "ws://").replace("https://", "wss://");
                if (!ws.endsWith("/ws")) {
                    ws = ws.replace("/chat", "/ws");
                    if (!ws.endsWith("/ws")) ws += "/ws";
                }
                if (ws.equals(serverUrl) && wsConnected.get()) {
                    Log.d(TAG, "Already connected to " + serverUrl);
                    return START_STICKY;
                }
                serverUrl = ws;
            }
        }

        if (serverUrl == null) {
            SharedPreferences prefs = getSharedPreferences("aion_prefs", MODE_PRIVATE);
            String saved = prefs.getString("saved_url", "http://192.168.x.x:8080/chat");
            serverUrl = saved.replace("http://", "ws://").replace("https://", "wss://")
                             .replace("/chat", "/ws");
        }

        Log.i(TAG, "onStartCommand url=" + serverUrl);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: 需要声明所有用到的前台服务类型
            int serviceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED) {
                serviceType |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
            }
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."), serviceType);
        } else {
            startForeground(NOTIF_FOREGROUND, buildKeepAlive("连接中..."));
        }

        shouldRun = true;
        startHeartbeatThread();
        startLocationThread();
        startActivityThread();
        return START_STICKY;
    }

    @Nullable @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        Log.i(TAG, "=== onDestroy ===");
        shouldRun = false;
        wsGeneration.incrementAndGet();
        if (heartbeatThread != null) heartbeatThread.interrupt();
        if (locationThread != null) locationThread.interrupt();
        if (activityThread != null) activityThread.interrupt();
        unregisterScreenReceiver();
        if (webSocket != null) try { webSocket.cancel(); } catch (Exception ignored) {}
        if (client != null) client.dispatcher().executorService().shutdown();
        stopMusic();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        unregisterNetworkCallback();
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        Log.w(TAG, "Task removed → schedule restart");
        Intent ri = new Intent(getApplicationContext(), AionPushService.class);
        ri.setPackage(getPackageName());
        PendingIntent pi = PendingIntent.getService(getApplicationContext(), 1, ri,
                PendingIntent.FLAG_ONE_SHOT | PendingIntent.FLAG_IMMUTABLE);
        AlarmManager am = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
        if (am != null) {
            am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    SystemClock.elapsedRealtime() + 3000, pi);
        }
        super.onTaskRemoved(rootIntent);
    }

    // ══════════════════════════════════════════════════════════
    //  网络变化监听 — 网络恢复时立即触发重连
    // ══════════════════════════════════════════════════════════

    private void registerNetworkCallback() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;

        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                Log.i(TAG, "★ Network available, connected=" + wsConnected.get());
                if (!wsConnected.get() && shouldRun) {
                    reconnectDelay = 3000;
                    connectWebSocket();
                }
            }
            @Override
            public void onLost(Network network) {
                Log.w(TAG, "★ Network lost");
                wsConnected.set(false);
                updateKeepAlive("网络断开，等待恢复...");
            }
        };

        NetworkRequest req = new NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        connectivityManager.registerNetworkCallback(req, networkCallback);
        Log.i(TAG, "NetworkCallback registered");
    }

    private void unregisterNetworkCallback() {
        if (connectivityManager != null && networkCallback != null) {
            try { connectivityManager.unregisterNetworkCallback(networkCallback); }
            catch (Exception ignored) {}
        }
    }

    // ══════════════════════════════════════════════════════════
    //  心跳线程 — 纯 Java Thread
    // ══════════════════════════════════════════════════════════

    private synchronized void startHeartbeatThread() {
        if (heartbeatThread != null && heartbeatThread.isAlive()) return;

        heartbeatThread = new Thread(() -> {
            Log.i(TAG, "♥ Heartbeat started tid=" + Thread.currentThread().getId());

            if (!wsConnected.get()) connectWebSocket();

            while (shouldRun) {
                try { Thread.sleep(HEARTBEAT_MS); }
                catch (InterruptedException e) { break; }
                if (!shouldRun) break;

                try {
                    if (wsConnected.get() && webSocket != null) {
                        boolean sent = webSocket.send("{\"type\":\"ping\"}");
                        long elapsed = (lastMessageTime > 0)
                                ? (System.currentTimeMillis() - lastMessageTime) / 1000 : 0;
                        Log.d(TAG, "♥ ping=" + sent + " msgs=" + msgReceived + " idle=" + elapsed + "s");

                        if (!sent) {
                            Log.w(TAG, "♥ ping failed → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        } else if (lastMessageTime > 0
                                && System.currentTimeMillis() - lastMessageTime > HEALTH_TIMEOUT) {
                            Log.w(TAG, "♥ health timeout → reconnect");
                            wsConnected.set(false);
                            connectWebSocket();
                        }
                    } else if (!wsConnected.get()) {
                        Log.i(TAG, "♥ not connected → reconnect");
                        connectWebSocket();
                    }
                } catch (Exception e) {
                    Log.e(TAG, "♥ error: " + e.getMessage());
                }
            }
            Log.i(TAG, "♥ Heartbeat exiting");
        }, "AionHeartbeat");
        heartbeatThread.setDaemon(false);
        heartbeatThread.start();
    }

    // ══════════════════════════════════════════════════════════
    //  定位上报线程 — 每隔 N 分钟获取 GPS 坐标并 POST 到服务器
    // ══════════════════════════════════════════════════════════

    private synchronized void startLocationThread() {
        if (locationThread != null && locationThread.isAlive()) return;

        locationThread = new Thread(() -> {
            Log.i(TAG, "📍 Location thread started");
            // 首次等 15 秒让 WS 和 GPS 稳定
            try { Thread.sleep(15000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    // 先检查服务端定位功能是否启用
                    checkLocationEnabled();
                    if (locationEnabled) {
                        requestLocationOnce();
                    } else {
                        Log.d(TAG, "📍 server location disabled, idle");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📍 error: " + e.getMessage());
                }

                long interval = locationEnabled ? locationInterval : LOCATION_INTERVAL_DISABLED;
                try { Thread.sleep(interval); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📍 Location thread exiting");
        }, "AionLocation");
        locationThread.setDaemon(false);
        locationThread.start();
    }

    private void checkLocationEnabled() {
        if (serverUrl == null) return;
        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");
        try {
            Request req = new Request.Builder()
                    .url(httpBase + "/api/location/config")
                    .get().build();
            try (Response resp = client.newCall(req).execute()) {
                if (resp.isSuccessful() && resp.body() != null) {
                    JSONObject cfg = new JSONObject(resp.body().string());
                    // active = enabled && 不在静默时段（服务端计算）
                    locationEnabled = cfg.optBoolean("active", false);
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📍 check config failed: " + e.getMessage());
        }
    }

    private void requestLocationOnce() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "📍 No location permission");
            return;
        }

        if (locationManager == null) {
            locationManager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        }
        if (locationManager == null) return;

        // 优先尝试 GPS，备用 Network
        Location loc = null;
        try {
            loc = locationManager.getLastKnownLocation(LocationManager.GPS_PROVIDER);
        } catch (Exception ignored) {}
        if (loc == null || System.currentTimeMillis() - loc.getTime() > 10 * 60_000) {
            try {
                loc = locationManager.getLastKnownLocation(LocationManager.NETWORK_PROVIDER);
            } catch (Exception ignored) {}
        }

        // 如果缓存的位置太旧（>10分钟），请求一次实时定位
        if (loc == null || System.currentTimeMillis() - loc.getTime() > 10 * 60_000) {
            requestFreshLocation();
            return;
        }

        lastKnownLocation = loc;
        postLocationToServer(loc);
    }

    private void requestFreshLocation() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) return;
        if (locationManager == null) return;

        // 注意: LocationListener 回调发生在 Looper 线程，这里用主线程 Looper
        try {
            String provider = locationManager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                    ? LocationManager.GPS_PROVIDER : LocationManager.NETWORK_PROVIDER;

            locationManager.requestSingleUpdate(provider, new LocationListener() {
                @Override
                public void onLocationChanged(Location location) {
                    lastKnownLocation = location;
                    postLocationToServer(location);
                }
                @Override public void onStatusChanged(String p, int s, Bundle e) {}
                @Override public void onProviderEnabled(String p) {}
                @Override public void onProviderDisabled(String p) {}
            }, getMainLooper());
        } catch (Exception e) {
            Log.e(TAG, "📍 requestSingleUpdate failed: " + e.getMessage());
        }
    }

    private void postLocationToServer(Location loc) {
        if (loc == null || serverUrl == null) return;

        // 从 wsUrl 推断 HTTP API 地址
        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");

        String apiUrl = httpBase + "/api/location/heartbeat";

        try {
            JSONObject body = new JSONObject();
            body.put("lng", loc.getLongitude());
            body.put("lat", loc.getLatitude());
            body.put("accuracy", loc.getAccuracy());
            body.put("is_gcj02", false);  // Android 原生 GPS 输出 WGS84

            MediaType JSON = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON);
            Request req = new Request.Builder().url(apiUrl).post(reqBody).build();

            // 同步请求（已在后台线程）
            try (Response resp = client.newCall(req).execute()) {
                String respBody = resp.body() != null ? resp.body().string() : "";
                Log.i(TAG, "📍 posted loc (" + String.format("%.4f,%.4f", loc.getLongitude(), loc.getLatitude())
                        + " acc=" + (int) loc.getAccuracy() + "m) → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📍 post failed: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  WebSocket 连接 — synchronized 防并发
    // ══════════════════════════════════════════════════════════

    private synchronized void connectWebSocket() {
        if (wsConnected.get()) return;
        if (serverUrl == null) { Log.e(TAG, "url=null"); return; }

        final int gen = wsGeneration.incrementAndGet();

        WebSocket old = webSocket;
        webSocket = null;
        if (old != null) try { old.cancel(); } catch (Exception ignored) {}

        Log.i(TAG, ">>> connect gen=" + gen + " → " + serverUrl);
        updateKeepAlive("连接中...");

        try {
            Request req = new Request.Builder().url(serverUrl).build();
            webSocket = client.newWebSocket(req, new WebSocketListener() {

                @Override
                public void onOpen(WebSocket ws, Response resp) {
                    if (gen != wsGeneration.get()) { ws.cancel(); return; }
                    Log.i(TAG, ">>> OPEN gen=" + gen);
                    wsConnected.set(true);
                    reconnectDelay = 3000;
                    msgReceived = 0;
                    lastMessageTime = System.currentTimeMillis();
                    updateKeepAlive("在线 ✨");
                }

                @Override
                public void onMessage(WebSocket ws, String text) {
                    if (gen != wsGeneration.get()) return;
                    lastMessageTime = System.currentTimeMillis();
                    handleMessage(text);
                }

                @Override
                public void onFailure(WebSocket ws, Throwable t, Response resp) {
                    if (gen != wsGeneration.get()) return;
                    String err = t != null ? t.getMessage() : "unknown";
                    Log.w(TAG, ">>> FAIL gen=" + gen + ": " + err);
                    wsConnected.set(false);
                    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
                    updateKeepAlive("连接失败: " + err);
                    // 不在这里阻塞或重连！心跳线程会处理
                }

                @Override
                public void onClosed(WebSocket ws, int code, String reason) {
                    if (gen != wsGeneration.get()) return;
                    Log.i(TAG, ">>> CLOSED gen=" + gen + " code=" + code);
                    wsConnected.set(false);
                    updateKeepAlive("连接关闭(" + code + ")");
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "connect error: " + e.getMessage());
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
        }
    }

    // ══════════════════════════════════════════════════════════
    //  消息 → 通知
    // ══════════════════════════════════════════════════════════

    private void handleMessage(String text) {
        try {
            JSONObject json = new JSONObject(text);
            String type = json.optString("type", "");

            if ("pong".equals(type) || "ping".equals(type)) return;

            msgReceived++;
            Log.d(TAG, "MSG #" + msgReceived + " type=" + type);

            JSONObject data = json.optJSONObject("data");

            switch (type) {
                case "schedule_alarm": {
                    String c = data != null ? data.optString("content", "闹铃") : "闹铃";
                    showNotif(CH_ALARM, "⏰ 闹铃", c, true);
                    break;
                }
                case "monitor_alert": {
                    String c = data != null ? data.optString("content", "监控提醒") : "监控提醒";
                    showNotif(CH_ALARM, "👁 监控", c, true);
                    break;
                }
                case "music": {
                    // 后台自动播放音乐（前台由 WebView JS 处理）
                    if (!isForegroundActive && data != null) {
                        JSONArray cards = data.optJSONArray("cards");
                        if (cards != null && cards.length() > 0) {
                            JSONObject firstCard = cards.optJSONObject(0);
                            if (firstCard != null) {
                                int songId = firstCard.optInt("id", 0);
                                if (songId > 0) {
                                    playMusicStream(songId);
                                }
                            }
                        }
                    }
                    break;
                }
                case "msg_created": {
                    if (data != null && !isForegroundActive) {
                        String role = data.optString("role", "");
                        if ("assistant".equals(role)) {
                            String c = data.optString("content", "");
                            if (c.length() > 100) c = c.substring(0, 100) + "...";
                            showNotif(CH_MESSAGE, "💬 Aion", c, false);
                        }
                    }
                    break;
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "parse error: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  原生音乐播放（后台 WebView 冻结时由 MediaPlayer 接管）
    // ══════════════════════════════════════════════════════════

    private void playMusicStream(int songId) {
        // ws://host:port/ws → http://host:port
        String httpBase = serverUrl.replace("ws://", "http://").replace("wss://", "https://");
        if (httpBase.endsWith("/ws")) httpBase = httpBase.substring(0, httpBase.length() - 3);
        String streamUrl = httpBase + "/api/music/stream/" + songId;
        Log.i(TAG, "♪ Playing music: " + streamUrl);

        stopMusic();

        try {
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .setUsage(AudioAttributes.USAGE_ALARM)  // 走闹钟音频流，可穿透勿扰模式
                    .build());
            mediaPlayer.setDataSource(streamUrl);
            mediaPlayer.setOnPreparedListener(MediaPlayer::start);
            mediaPlayer.setOnCompletionListener(mp -> {
                Log.i(TAG, "♪ Music finished");
                mp.release();
                if (mediaPlayer == mp) mediaPlayer = null;
            });
            mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                Log.e(TAG, "♪ MediaPlayer error: " + what + "/" + extra);
                mp.release();
                if (mediaPlayer == mp) mediaPlayer = null;
                return true;
            });
            mediaPlayer.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "♪ Music play error: " + e.getMessage());
            if (mediaPlayer != null) {
                try { mediaPlayer.release(); } catch (Exception ignored) {}
                mediaPlayer = null;
            }
        }
    }

    private void stopMusic() {
        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) mediaPlayer.stop();
                mediaPlayer.release();
            } catch (Exception ignored) {}
            mediaPlayer = null;
        }
    }

    private void showNotif(String ch, String title, String text, boolean high) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        Log.i(TAG, "NOTIFY " + title + ": " + text);

        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, notifCounter, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder b = new NotificationCompat.Builder(this, ch)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setPriority(high ? NotificationCompat.PRIORITY_HIGH : NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setCategory(high ? NotificationCompat.CATEGORY_ALARM : NotificationCompat.CATEGORY_MESSAGE)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC);

        if (high) {
            b.setDefaults(NotificationCompat.DEFAULT_ALL);
            b.setFullScreenIntent(pi, true);  // 锁屏时亮屏弹出
        }

        nm.notify(NOTIF_MSG_BASE + (notifCounter++ % 50), b.build());
    }

    // ══════════════════════════════════════════════════════════
    //  通知渠道
    // ══════════════════════════════════════════════════════════

    private void createNotificationChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;

        NotificationChannel c1 = new NotificationChannel(CH_KEEPALIVE, "Aion Oloth 保活",
                NotificationManager.IMPORTANCE_LOW);
        c1.setShowBadge(false);
        nm.createNotificationChannel(c1);

        NotificationChannel c2 = new NotificationChannel(CH_MESSAGE, "Aion Oloth 消息",
                NotificationManager.IMPORTANCE_DEFAULT);
        nm.createNotificationChannel(c2);

        NotificationChannel c3 = new NotificationChannel(CH_ALARM, "闹铃与监控",
                NotificationManager.IMPORTANCE_HIGH);
        c3.enableVibration(true);
        c3.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(c3);
    }

    private Notification buildKeepAlive(String text) {
        Intent i = new Intent(this, LauncherActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CH_KEEPALIVE)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("Aion Oloth")
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void updateKeepAlive(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIF_FOREGROUND, buildKeepAlive(text));
    }

    // ══════════════════════════════════════════════════════════
    //  活动上报线程 — UsageStatsManager 检测前台应用
    // ══════════════════════════════════════════════════════════

    private synchronized void startActivityThread() {
        if (activityThread != null && activityThread.isAlive()) return;

        // 注册屏幕开关广播
        registerScreenReceiver();

        activityThread = new Thread(() -> {
            Log.i(TAG, "📱 Activity thread started");
            // 等待 20 秒让服务稳定
            try { Thread.sleep(20000); } catch (InterruptedException e) { return; }

            while (shouldRun) {
                try {
                    if (hasUsageStatsPermission()) {
                        reportForegroundApp();
                    } else {
                        Log.d(TAG, "📱 Usage access permission not granted");
                    }
                } catch (Exception e) {
                    Log.e(TAG, "📱 activity error: " + e.getMessage());
                }

                try { Thread.sleep(ACTIVITY_INTERVAL); }
                catch (InterruptedException e) { break; }
            }
            Log.i(TAG, "📱 Activity thread exiting");
        }, "AionActivity");
        activityThread.setDaemon(false);
        activityThread.start();
    }

    private boolean hasUsageStatsPermission() {
        try {
            UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
            if (usm == null) return false;
            long now = System.currentTimeMillis();
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 60_000, now);
            return stats != null && !stats.isEmpty();
        } catch (Exception e) {
            return false;
        }
    }

    private void reportForegroundApp() {
        UsageStatsManager usm = (UsageStatsManager) getSystemService(Context.USAGE_STATS_SERVICE);
        if (usm == null) return;

        long now = System.currentTimeMillis();

        // 方案一：UsageEvents（更可靠，能在后台获取真实的前台切换事件）
        String pkgName = null;
        try {
            UsageEvents events = usm.queryEvents(now - 120_000, now);
            UsageEvents.Event event = new UsageEvents.Event();
            while (events.hasNextEvent()) {
                events.getNextEvent(event);
                // ACTIVITY_RESUMED (=1 on older / =2) 表示 Activity 进入前台
                if (event.getEventType() == UsageEvents.Event.ACTIVITY_RESUMED
                        || event.getEventType() == 1) {
                    pkgName = event.getPackageName();
                }
            }
        } catch (Exception e) {
            Log.d(TAG, "📱 UsageEvents failed, fallback to queryUsageStats: " + e.getMessage());
        }

        // 方案二：如果 UsageEvents 没结果，fallback 到 queryUsageStats
        if (pkgName == null) {
            java.util.List<UsageStats> stats = usm.queryUsageStats(
                    UsageStatsManager.INTERVAL_DAILY, now - 120_000, now);
            if (stats != null && !stats.isEmpty()) {
                UsageStats recent = null;
                for (UsageStats s : stats) {
                    if (recent == null || s.getLastTimeUsed() > recent.getLastTimeUsed()) {
                        recent = s;
                    }
                }
                if (recent != null) pkgName = recent.getPackageName();
            }
        }

        if (pkgName == null) return;

        // 仅过滤自身
        if (pkgName.equals(getPackageName())) {
            return;
        }

        // 每次轮询都上报（服务端摘要层负责合并去重）
        lastReportedApp = pkgName;
        lastReportedTime = now;

        // 直接发送包名，服务端做名称翻译（避免 vivo ROM 中文编码乱码）
        postActivityToServer(pkgName);
    }

    private void postActivityToServer(String pkgName) {
        if (serverUrl == null) return;

        String httpBase = serverUrl
                .replace("ws://", "http://")
                .replace("wss://", "https://")
                .replace("/ws", "");

        try {
            JSONObject body = new JSONObject();
            body.put("device", "phone");
            body.put("app", pkgName);
            body.put("title", pkgName);
            body.put("timestamp", System.currentTimeMillis() / 1000.0);

            MediaType JSON_TYPE = MediaType.get("application/json; charset=utf-8");
            RequestBody reqBody = RequestBody.create(body.toString(), JSON_TYPE);
            Request req = new Request.Builder()
                    .url(httpBase + "/api/activity/report")
                    .post(reqBody)
                    .build();

            try (Response resp = client.newCall(req).execute()) {
                Log.i(TAG, "📱 reported activity: " + pkgName + " → " + resp.code());
            }
        } catch (Exception e) {
            Log.e(TAG, "📱 activity report failed: " + e.getMessage());
        }
    }

    // ══════════════════════════════════════════════════════════
    //  屏幕开关监听 — 锁屏/亮屏时立即上报
    // ══════════════════════════════════════════════════════════

    private void registerScreenReceiver() {
        if (screenReceiver != null) return;
        screenReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (intent == null || intent.getAction() == null) return;
                switch (intent.getAction()) {
                    case Intent.ACTION_SCREEN_OFF:
                        Log.i(TAG, "📱 Screen OFF");
                        screenOn = false;
                        lastReportedApp = "__screen_off__";
                        // 在后台线程发送，避免阻塞广播
                        new Thread(() -> postActivityToServer("screen_off"), "ScreenOff").start();
                        break;
                    case Intent.ACTION_SCREEN_ON:
                        Log.i(TAG, "📱 Screen ON");
                        screenOn = true;
                        lastReportedApp = "__screen_on__";
                        new Thread(() -> postActivityToServer("screen_on"), "ScreenOn").start();
                        break;
                }
            }
        };
        IntentFilter filter = new IntentFilter();
        filter.addAction(Intent.ACTION_SCREEN_OFF);
        filter.addAction(Intent.ACTION_SCREEN_ON);
        registerReceiver(screenReceiver, filter);
        Log.i(TAG, "📱 Screen receiver registered");
    }

    private void unregisterScreenReceiver() {
        if (screenReceiver != null) {
            try { unregisterReceiver(screenReceiver); } catch (Exception ignored) {}
            screenReceiver = null;
        }
    }
}
