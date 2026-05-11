package com.aion.chat;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.util.Base64;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import java.io.File;
import java.io.FileInputStream;
import java.nio.ByteBuffer;

/**
 * 原生视频录制桥接 — 复用 CameraBridge 的预览帧 + AudioBridge 的 PCM 帧
 * 使用 MediaCodec(H.264+AAC) + MediaMuxer 生成 MP4，录制期间预览不中断
 *
 * 前端调用: window.AionVideo.startRecord(width, height) / stopRecord() / cancel() / isRecording()
 * 数据来源: CameraBridge.onVideoFrame() 和 AudioBridge.onAudioFrame() 自动推送
 */
public class VideoBridge {

    private static final String TAG = "VideoBridge";
    private static final int VIDEO_BITRATE = 1_500_000;
    private static final int VIDEO_FPS = 15;
    private static final int I_FRAME_INTERVAL = 1;
    private static final int AUDIO_SAMPLE_RATE = 16000;
    private static final int AUDIO_BITRATE = 64000;

    private final WebView webView;
    private final File cacheDir;

    private MediaCodec videoEncoder;
    private MediaCodec audioEncoder;
    private MediaMuxer muxer;

    private volatile boolean recording = false;
    private int videoTrackIndex = -1;
    private int audioTrackIndex = -1;
    private volatile boolean muxerStarted = false;
    private boolean videoFormatReceived = false;
    private boolean audioFormatReceived = false;

    private long startTimeNs = 0;
    private File outputFile;
    private int recordWidth;
    private int recordHeight;

    // 可复用的 NV12 转换缓冲区
    private byte[] nv12Buf;

    // 编码器输出 buffer info（复用避免 GC）
    private final MediaCodec.BufferInfo videoBufInfo = new MediaCodec.BufferInfo();
    private final MediaCodec.BufferInfo audioBufInfo = new MediaCodec.BufferInfo();

    public VideoBridge(WebView webView, File cacheDir) {
        this.webView = webView;
        this.cacheDir = cacheDir;
    }

    @JavascriptInterface
    public boolean startRecord(int width, int height) {
        if (recording) return false;

        recordWidth = width > 0 ? width : 480;
        recordHeight = height > 0 ? height : 640;
        // 编码器要求偶数尺寸
        recordWidth = (recordWidth / 2) * 2;
        recordHeight = (recordHeight / 2) * 2;

        try {
            outputFile = new File(cacheDir, "vc_" + System.currentTimeMillis() + ".mp4");

            // ── 视频编码器 (H.264) ──
            MediaFormat vFmt = MediaFormat.createVideoFormat(
                    MediaFormat.MIMETYPE_VIDEO_AVC, recordWidth, recordHeight);
            vFmt.setInteger(MediaFormat.KEY_BIT_RATE, VIDEO_BITRATE);
            vFmt.setInteger(MediaFormat.KEY_FRAME_RATE, VIDEO_FPS);
            vFmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, I_FRAME_INTERVAL);
            vFmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar);

            videoEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            videoEncoder.configure(vFmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            videoEncoder.start();

            // ── 音频编码器 (AAC) ──
            MediaFormat aFmt = MediaFormat.createAudioFormat(
                    MediaFormat.MIMETYPE_AUDIO_AAC, AUDIO_SAMPLE_RATE, 1);
            aFmt.setInteger(MediaFormat.KEY_BIT_RATE, AUDIO_BITRATE);
            aFmt.setInteger(MediaFormat.KEY_AAC_PROFILE,
                    MediaCodecInfo.CodecProfileLevel.AACObjectLC);

            audioEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC);
            audioEncoder.configure(aFmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            audioEncoder.start();

            // ── Muxer ──
            muxer = new MediaMuxer(outputFile.getAbsolutePath(),
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);

            videoTrackIndex = -1;
            audioTrackIndex = -1;
            muxerStarted = false;
            videoFormatReceived = false;
            audioFormatReceived = false;

            // 分配 NV12 转换缓冲区
            nv12Buf = new byte[recordWidth * recordHeight * 3 / 2];

            startTimeNs = System.nanoTime();
            recording = true;

            Log.i(TAG, "Recording started: " + recordWidth + "x" + recordHeight);
            return true;
        } catch (Exception e) {
            Log.e(TAG, "startRecord failed", e);
            cleanup();
            return false;
        }
    }

    /**
     * 由 CameraBridge 的编码线程调用，推送已旋转的 NV21 帧
     */
    public void onVideoFrame(byte[] nv21, int width, int height) {
        if (!recording || videoEncoder == null) return;
        // 尺寸不匹配时跳过（理论上不应发生）
        if (width != recordWidth || height != recordHeight) return;

        try {
            int idx = videoEncoder.dequeueInputBuffer(0);
            if (idx < 0) return; // 编码器繁忙，丢帧

            ByteBuffer inBuf = videoEncoder.getInputBuffer(idx);
            if (inBuf == null) return;

            // NV21 → NV12（仅交换 chroma 平面的 U/V 字节对）
            int ySize = width * height;
            System.arraycopy(nv21, 0, nv12Buf, 0, ySize);
            for (int i = ySize; i < nv21.length - 1; i += 2) {
                nv12Buf[i] = nv21[i + 1];     // U
                nv12Buf[i + 1] = nv21[i];     // V
            }

            inBuf.clear();
            inBuf.put(nv12Buf, 0, Math.min(nv12Buf.length, inBuf.remaining()));

            long pts = (System.nanoTime() - startTimeNs) / 1000;
            videoEncoder.queueInputBuffer(idx, 0, nv12Buf.length, pts, 0);

            drainEncoder(videoEncoder, videoBufInfo, true);
        } catch (Exception e) {
            Log.w(TAG, "onVideoFrame error", e);
        }
    }

    /**
     * 由 AudioBridge 的录音线程调用，推送 PCM 16-bit mono 帧
     */
    public void onAudioFrame(byte[] pcm, int length) {
        if (!recording || audioEncoder == null) return;

        try {
            int idx = audioEncoder.dequeueInputBuffer(0);
            if (idx < 0) return;

            ByteBuffer inBuf = audioEncoder.getInputBuffer(idx);
            if (inBuf == null) return;

            inBuf.clear();
            inBuf.put(pcm, 0, Math.min(length, inBuf.remaining()));

            long pts = (System.nanoTime() - startTimeNs) / 1000;
            audioEncoder.queueInputBuffer(idx, 0, length, pts, 0);

            drainEncoder(audioEncoder, audioBufInfo, false);
        } catch (Exception e) {
            Log.w(TAG, "onAudioFrame error", e);
        }
    }

    @JavascriptInterface
    public String stopRecord() {
        if (!recording) return null;
        recording = false;

        try {
            // 发送 EOS 信号
            sendEOS(videoEncoder);
            drainEncoder(videoEncoder, videoBufInfo, true);
            sendEOS(audioEncoder);
            drainEncoder(audioEncoder, audioBufInfo, false);

            if (muxerStarted) {
                muxer.stop();
            }
            cleanup();

            // 读取文件转 base64
            if (outputFile != null && outputFile.exists()) {
                byte[] data = readFile(outputFile);
                String b64 = Base64.encodeToString(data, Base64.NO_WRAP);
                long size = data.length;
                outputFile.delete();
                Log.i(TAG, "Recording stopped, size: " + size + " bytes");
                return b64;
            }
        } catch (Exception e) {
            Log.e(TAG, "stopRecord error", e);
            cleanup();
        }
        return null;
    }

    @JavascriptInterface
    public void cancel() {
        recording = false;
        cleanup();
        if (outputFile != null && outputFile.exists()) {
            outputFile.delete();
        }
        Log.i(TAG, "Recording cancelled");
    }

    @JavascriptInterface
    public boolean isRecording() {
        return recording;
    }

    // ── 内部方法 ──

    private void sendEOS(MediaCodec encoder) {
        if (encoder == null) return;
        try {
            int idx = encoder.dequeueInputBuffer(5000);
            if (idx >= 0) {
                encoder.queueInputBuffer(idx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
            }
        } catch (Exception ignored) {}
    }

    /**
     * 排空编码器输出，写入 Muxer
     * @param isVideo true=视频轨道, false=音频轨道
     */
    private void drainEncoder(MediaCodec encoder, MediaCodec.BufferInfo info, boolean isVideo) {
        while (true) {
            int outIdx = encoder.dequeueOutputBuffer(info, 0);
            if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                synchronized (this) {
                    if (isVideo) {
                        videoTrackIndex = muxer.addTrack(encoder.getOutputFormat());
                        videoFormatReceived = true;
                    } else {
                        audioTrackIndex = muxer.addTrack(encoder.getOutputFormat());
                        audioFormatReceived = true;
                    }
                    if (videoFormatReceived && audioFormatReceived && !muxerStarted) {
                        muxer.start();
                        muxerStarted = true;
                        Log.i(TAG, "Muxer started");
                    }
                }
            } else if (outIdx >= 0) {
                ByteBuffer buf = encoder.getOutputBuffer(outIdx);
                if (buf != null && muxerStarted && info.size > 0) {
                    buf.position(info.offset);
                    buf.limit(info.offset + info.size);
                    int trackIdx = isVideo ? videoTrackIndex : audioTrackIndex;
                    synchronized (this) {
                        muxer.writeSampleData(trackIdx, buf, info);
                    }
                }
                encoder.releaseOutputBuffer(outIdx, false);
                if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break;
            } else {
                break;
            }
        }
    }

    private void cleanup() {
        try { if (videoEncoder != null) { videoEncoder.stop(); videoEncoder.release(); } }
        catch (Exception ignored) {}
        try { if (audioEncoder != null) { audioEncoder.stop(); audioEncoder.release(); } }
        catch (Exception ignored) {}
        try { if (muxer != null) muxer.release(); }
        catch (Exception ignored) {}
        videoEncoder = null;
        audioEncoder = null;
        muxer = null;
    }

    private static byte[] readFile(File f) {
        try (FileInputStream fis = new FileInputStream(f)) {
            byte[] data = new byte[(int) f.length()];
            int read = 0;
            while (read < data.length) {
                int n = fis.read(data, read, data.length - read);
                if (n <= 0) break;
                read += n;
            }
            return data;
        } catch (Exception e) {
            return new byte[0];
        }
    }
}
