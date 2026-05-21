package com.aion.chat;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * 原生 BLE 桥接 — 绕过 WebView 不支持 Web Bluetooth API 的限制
 * 扫描并连接 SOSEXY 设备，实现与 chat.html 中 toySendData2 相同的封包协议
 *
 * 前端调用: window.AionBle.connect() / disconnect() / isConnected() / sendData(hex)
 * 前端回调: toyNativeBle.onConnected() / onDisconnected() / onError(msg) / onLog(msg)
 */
@SuppressLint("MissingPermission")
public class BleBridge {

    private static final String TAG = "AionBle";
    private static final UUID SERVICE_UUID = UUID.fromString("0000ee01-0000-1000-8000-00805f9b34fb");
    private static final UUID WRITE_UUID   = UUID.fromString("0000ee03-0000-1000-8000-00805f9b34fb");
    private static final UUID NOTIFY_UUID  = UUID.fromString("0000ee02-0000-1000-8000-00805f9b34fb");
    private static final UUID CCCD_UUID    = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private final WebView webView;
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService writeExecutor = Executors.newSingleThreadExecutor();

    private BluetoothAdapter adapter;
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic writeChar;
    private volatile boolean connected = false;
    private volatile boolean scanning = false;
    private volatile CountDownLatch writeLatch;

    public BleBridge(WebView webView, Context context) {
        this.webView = webView;
        this.context = context;
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm != null) adapter = bm.getAdapter();
    }

    // ── JS 接口 ──

    @JavascriptInterface
    public void connect() {
        if (adapter == null || !adapter.isEnabled()) {
            callJs("toyNativeBle.onError('蓝牙未开启')");
            return;
        }
        if (connected || scanning) return;

        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            callJs("toyNativeBle.onError('无法获取BLE扫描器')");
            return;
        }

        scanning = true;
        callJs("toyNativeBle.onLog('搜索中...')");

        try {
            scanner.startScan(scanCb);
        } catch (Exception e) {
            scanning = false;
            callJs("toyNativeBle.onError('扫描失败: " + escapeJs(e.getMessage()) + "')");
            return;
        }

        // 10秒超时
        mainHandler.postDelayed(() -> {
            if (scanning) {
                stopScan();
                callJs("toyNativeBle.onError('未找到设备')");
            }
        }, 10000);
    }

    @JavascriptInterface
    public void disconnect() {
        stopScan();
        connected = false;
        writeChar = null;
        if (gatt != null) {
            try { gatt.disconnect(); gatt.close(); } catch (Exception ignored) {}
            gatt = null;
        }
    }

    @JavascriptInterface
    public boolean isConnected() {
        return connected;
    }

    /**
     * 发送控制指令（hex 字符串），内部实现 sendData2 封包协议：
     * "00" 前缀 → 18字节分块 → [random, index] 包头 → 逐包写入
     */
    @JavascriptInterface
    public void sendData(final String hexCmd) {
        if (!connected || writeChar == null) return;
        writeExecutor.execute(() -> sendDataInternal(hexCmd));
    }

    /**
     * 发送 BLE 广播（用于广播类玩具控制）
     * payload 由后端 ble_core 构建，前端直接传入 hex 字符串
     */
    @JavascriptInterface
    public void sendAdvertise(final String payloadHex, final int durationMs) {
        if (adapter == null || !adapter.isEnabled()) {
            callJs("toyNativeBle.onError('蓝牙未开启')");
            return;
        }
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S
                && context.checkSelfPermission("android.permission.BLUETOOTH_ADVERTISE")
                   != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            callJs("toyNativeBle.onError('缺少蓝牙广播权限，请在系统设置中授权')");
            return;
        }
        BluetoothLeAdvertiser advertiser = adapter.getBluetoothLeAdvertiser();
        if (advertiser == null) {
            callJs("toyNativeBle.onError('此设备不支持BLE广播')");
            return;
        }

        byte[] payload = hexToBytes(payloadHex);

        AdvertiseSettings settings = new AdvertiseSettings.Builder()
                .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                .setConnectable(false)
                .setTimeout(Math.min(durationMs, 10000))
                .build();

        AdvertiseData data = new AdvertiseData.Builder()
                .setIncludeDeviceName(false)
                .setIncludeTxPowerLevel(false)
                .addManufacturerData(0xFFFF, payload)
                .build();

        advertiser.startAdvertising(settings, data, new AdvertiseCallback() {
            @Override
            public void onStartSuccess(AdvertiseSettings settingsInEffect) {
                callJs("toyNativeBle.onLog('📡 广播已发射 (" + durationMs + "ms)')");
            }

            @Override
            public void onStartFailure(int errorCode) {
                callJs("toyNativeBle.onError('广播失败: code=" + errorCode + "')");
            }
        });
    }

    // ── BLE 扫描 ──

    private final ScanCallback scanCb = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            if (!scanning) return;
            BluetoothDevice dev = result.getDevice();
            String name = null;
            try { name = dev.getName(); } catch (Exception ignored) {}
            if (name != null && name.startsWith("SOSEXY")) {
                stopScan();
                callJs("toyNativeBle.onLog('" + escapeJs(name) + "')");
                connectGatt(dev);
            }
        }
    };

    private void stopScan() {
        if (!scanning) return;
        scanning = false;
        try { if (scanner != null) scanner.stopScan(scanCb); } catch (Exception ignored) {}
    }

    // ── GATT 连接 ──

    private void connectGatt(BluetoothDevice dev) {
        try {
            gatt = dev.connectGatt(context, false, gattCb, BluetoothDevice.TRANSPORT_LE);
        } catch (Exception e) {
            callJs("toyNativeBle.onError('连接失败')");
        }
    }

    @SuppressWarnings("deprecation")
    private final BluetoothGattCallback gattCb = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                g.discoverServices();
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                connected = false;
                writeChar = null;
                callJs("toyNativeBle.onDisconnected()");
                try { g.close(); } catch (Exception ignored) {}
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int status) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                callJs("toyNativeBle.onError('服务发现失败')");
                return;
            }
            BluetoothGattService svc = g.getService(SERVICE_UUID);
            if (svc == null) {
                callJs("toyNativeBle.onError('未找到BLE服务')");
                return;
            }
            writeChar = svc.getCharacteristic(WRITE_UUID);
            if (writeChar == null) {
                callJs("toyNativeBle.onError('未找到写入特征')");
                return;
            }
            // 根据特征属性设置写入类型
            if ((writeChar.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) {
                writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            } else {
                writeChar.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
            }
            // 订阅通知
            BluetoothGattCharacteristic nc = svc.getCharacteristic(NOTIFY_UUID);
            if (nc != null) {
                g.setCharacteristicNotification(nc, true);
                BluetoothGattDescriptor desc = nc.getDescriptor(CCCD_UUID);
                if (desc != null) {
                    desc.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
                    g.writeDescriptor(desc);
                }
            }
            connected = true;
            callJs("toyNativeBle.onConnected()");
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            CountDownLatch l = writeLatch;
            if (l != null) l.countDown();
        }
    };

    // ── 数据发送（sendData2 封包协议） ──

    @SuppressWarnings("deprecation")
    private void sendDataInternal(String hexCmd) {
        if (gatt == null || writeChar == null) return;
        try {
            byte[] data = hexToBytes("00" + hexCmd);
            int chunkSize = 18;
            int numChunks = Math.max(1, (data.length + chunkSize - 1) / chunkSize);
            int rnd = (int) (Math.random() * 255);

            for (int i = 0; i < numChunks; i++) {
                int start = i * chunkSize;
                int end = Math.min(start + chunkSize, data.length);
                byte[] pkt = new byte[2 + (end - start)];
                pkt[0] = (byte) rnd;
                pkt[1] = (byte) (i + 1);
                System.arraycopy(data, start, pkt, 2, end - start);

                if (!writeChunk(pkt)) {
                    Log.w(TAG, "writeChunk timeout at chunk " + i);
                    return;
                }
            }
            // 末块恰好 18 字节时追加终止包
            if (data.length > 0 && data.length % chunkSize == 0) {
                writeChunk(new byte[]{(byte) rnd, (byte) (numChunks + 1)});
            }
        } catch (Exception e) {
            Log.e(TAG, "sendData error", e);
        }
    }

    @SuppressWarnings("deprecation")
    private boolean writeChunk(byte[] value) throws InterruptedException {
        writeLatch = new CountDownLatch(1);
        writeChar.setValue(value);
        gatt.writeCharacteristic(writeChar);
        return writeLatch.await(2, TimeUnit.SECONDS);
    }

    // ── 工具方法 ──

    private byte[] hexToBytes(String hex) {
        int len = hex.length();
        byte[] out = new byte[len / 2];
        for (int i = 0; i < len; i += 2)
            out[i / 2] = (byte) ((Character.digit(hex.charAt(i), 16) << 4)
                    + Character.digit(hex.charAt(i + 1), 16));
        return out;
    }

    private void callJs(String js) {
        mainHandler.post(() -> webView.evaluateJavascript(
                "typeof toyNativeBle!=='undefined'&&" + js, null));
    }

    private String escapeJs(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("'", "\\'");
    }
}
