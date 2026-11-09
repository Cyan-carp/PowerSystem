package com.powersystem.mobile;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 平台手机客户端外壳：一个 Activity + 一个 WebView，加载线上网页。
 *
 * 刻意不引第三方依赖（不含 AndroidX）：本工程用 aapt2/javac/d8/apksigner 直接构建，
 * 不走 Gradle 拉依赖。界面全部用代码搭，避免 layout 资源与 R 引用。
 *
 * 能力边界：
 * - 站内 http/https 一律留在 WebView；站外链接交给系统浏览器；tel/mailto 交给系统。
 * - 返回键先退网页历史，退到底再按一次才退出应用。
 * - 下载（如报表 CSV）交给系统 DownloadManager，并带上登录 Cookie。
 * - 服务器地址存在 SharedPreferences；首次启动先确认，之后长按桌面图标可再改。
 */
public class MainActivity extends Activity {

    /** 桌面图标长按「服务器设置」使用的动作，与 AndroidManifest 的 intent-filter 对齐 */
    private static final String ACTION_SETTINGS = "com.powersystem.mobile.SETTINGS";

    private static final String PREFS = "powersystem_mobile";
    private static final String KEY_URL = "server_url";
    /** 生产入口；允许在设置页改成内网/测试地址 */
    private static final String DEFAULT_URL = "https://www.cyancarp.cn/";

    private static final int COLOR_BG = 0xFFEEF1F7;
    private static final int COLOR_INK = 0xFF0B1322;
    private static final int COLOR_INK_3 = 0xFF5A6478;

    private FrameLayout root;
    /** 网页舞台：WebView + 顶部进度条，创建后常驻，切页不重建（保留登录态与网页历史） */
    private FrameLayout stage;
    private WebView web;
    private ProgressBar bar;
    /** 覆盖层：同一时刻只留一个 */
    private View errorView;
    private View settingsView;
    private SharedPreferences prefs;
    private long lastBackPressedAt = 0L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        root = new FrameLayout(this);
        root.setBackgroundColor(COLOR_BG);
        setContentView(root);

        if (ACTION_SETTINGS.equals(getIntent().getAction())) {
            showSettings();
            return;
        }
        String saved = prefs.getString(KEY_URL, "");
        if (saved == null || saved.length() == 0) {
            /* 首次启动不猜地址：先让人看一眼生产入口再决定 */
            showSettings();
        } else {
            load(saved);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && ACTION_SETTINGS.equals(intent.getAction())) {
            showSettings();
        }
    }

    /* ==================== WebView ==================== */

    private void ensureWeb() {
        if (web != null) return;

        stage = new FrameLayout(this);
        web = new WebView(this);
        web.setBackgroundColor(COLOR_BG);
        stage.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        FrameLayout.LayoutParams barParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(2));
        barParams.gravity = Gravity.TOP;
        stage.addView(bar, barParams);

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        /* 网页自带 viewport 与响应式断点，缩放交给页面自己控制更稳 */
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        /* 生产是 HTTPS：禁止 https 页面混载 http 资源 */
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        /* 不读本地文件：网页只需要网络与本地存储 */
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setUserAgentString(settings.getUserAgentString() + " PowerSystemApp/1.0");

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (uri == null) return false;
                String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
                if ("http".equals(scheme) || "https".equals(scheme)) {
                    if (sameHost(uri)) return false;
                    openOutside(uri);
                    return true;
                }
                openOutside(uri);
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (bar != null) {
                    bar.setVisibility(View.GONE);
                    bar.setProgress(0);
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                /* 只有主文档失败才换错误页：图片/接口失败由网页自己提示 */
                if (request.isForMainFrame()) {
                    showError(error != null ? String.valueOf(error.getDescription()) : "");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int progress) {
                if (bar == null) return;
                bar.setVisibility(progress >= 100 ? View.GONE : View.VISIBLE);
                bar.setProgress(progress);
            }
        });

        /* 报表 CSV 等下载：WebView 自己不会存盘，交给系统下载器并带上 Cookie */
        web.setDownloadListener(new android.webkit.DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                try {
                    String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
                    DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                    String cookie = CookieManager.getInstance().getCookie(url);
                    if (cookie != null && cookie.length() > 0) {
                        request.addRequestHeader("Cookie", cookie);
                    }
                    request.addRequestHeader("User-Agent", userAgent);
                    request.setMimeType(mimeType);
                    request.setTitle(fileName);
                    request.setNotificationVisibility(
                            DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
                    DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    if (manager != null) manager.enqueue(request);
                    Toast.makeText(MainActivity.this, "已开始下载：" + fileName, Toast.LENGTH_SHORT).show();
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                }
            }
        });

        root.addView(stage, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private boolean sameHost(Uri uri) {
        String host = uri.getHost();
        if (host == null) return false;
        String current = web != null && web.getUrl() != null ? Uri.parse(web.getUrl()).getHost() : null;
        if (current == null) {
            /* 首次加载时 getUrl() 还没有值，按配置入口判断 */
            String saved = prefs.getString(KEY_URL, DEFAULT_URL);
            return host.equalsIgnoreCase(String.valueOf(Uri.parse(saved).getHost()))
                    || host.equalsIgnoreCase(String.valueOf(Uri.parse(DEFAULT_URL).getHost()));
        }
        return host.equalsIgnoreCase(current);
    }

    private void openOutside(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, "没有可以打开该链接的应用", Toast.LENGTH_SHORT).show();
        }
    }

    private void load(String url) {
        ensureWeb();
        dropOverlays();
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    /* ==================== 覆盖层：错误页 / 服务器设置 ==================== */

    private void dropOverlays() {
        if (errorView != null) {
            root.removeView(errorView);
            errorView = null;
        }
        if (settingsView != null) {
            root.removeView(settingsView);
            settingsView = null;
        }
    }

    private void showError(String reason) {
        if (errorView != null) return;
        if (settingsView != null) {
            root.removeView(settingsView);
            settingsView = null;
        }
        if (web != null) web.setVisibility(View.GONE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setPadding(dp(28), dp(28), dp(28), dp(28));
        box.setBackgroundColor(COLOR_BG);

        TextView title = new TextView(this);
        title.setText("连接不上平台");
        title.setTextColor(COLOR_INK);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f);
        title.setGravity(Gravity.CENTER);
        box.addView(title);

        String current = prefs.getString(KEY_URL, DEFAULT_URL);
        TextView detail = new TextView(this);
        detail.setText("地址：" + current
                + (reason == null || reason.length() == 0 ? "" : "\n原因：" + reason)
                + "\n\n请确认手机已联网，或检查服务器地址是否正确。");
        detail.setTextColor(COLOR_INK_3);
        detail.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f);
        detail.setGravity(Gravity.CENTER);
        detail.setPadding(0, dp(10), 0, dp(22));
        box.addView(detail);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);

        Button retry = new Button(this);
        retry.setText("重试");
        retry.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                load(prefs.getString(KEY_URL, DEFAULT_URL));
            }
        });
        row.addView(retry);

        Button change = new Button(this);
        change.setText("修改地址");
        change.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showSettings();
            }
        });
        LinearLayout.LayoutParams changeParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        changeParams.leftMargin = dp(12);
        row.addView(change, changeParams);

        box.addView(row);

        errorView = box;
        root.addView(errorView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void showSettings() {
        if (settingsView != null) return;
        if (errorView != null) {
            root.removeView(errorView);
            errorView = null;
        }
        if (web != null) web.setVisibility(View.GONE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(COLOR_BG);
        box.setPadding(dp(24), dp(34), dp(24), dp(24));

        TextView title = new TextView(this);
        title.setText("服务器地址");
        title.setTextColor(COLOR_INK);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 19f);
        box.addView(title);

        TextView hint = new TextView(this);
        hint.setText("默认是生产入口。需要连内网或测试环境时改成对应地址，改完点「保存并进入」。");
        hint.setTextColor(COLOR_INK_3);
        hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12.5f);
        hint.setPadding(0, dp(8), 0, dp(16));
        box.addView(hint);

        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(prefs.getString(KEY_URL, DEFAULT_URL));
        input.setTextColor(COLOR_INK);
        input.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f);
        box.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView note = new TextView(this);
        note.setText("以后想改地址：长按桌面上的应用图标 → 服务器设置。");
        note.setTextColor(COLOR_INK_3);
        note.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11.5f);
        note.setPadding(0, dp(14), 0, dp(20));
        box.addView(note);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button reset = new Button(this);
        reset.setText("恢复默认");
        reset.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                input.setText(DEFAULT_URL);
            }
        });
        row.addView(reset);

        Button save = new Button(this);
        save.setText("保存并进入");
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String url = normalize(input.getText().toString());
                if (url == null) {
                    Toast.makeText(MainActivity.this, "地址格式不对，请填 https:// 开头的完整地址", Toast.LENGTH_LONG).show();
                    return;
                }
                prefs.edit().putString(KEY_URL, url).apply();
                load(url);
            }
        });
        LinearLayout.LayoutParams saveParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        saveParams.leftMargin = dp(12);
        row.addView(save, saveParams);

        box.addView(row);

        settingsView = box;
        root.addView(settingsView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    /** 只接受 http/https；没写协议时补 https，主机为空则拒绝 */
    private String normalize(String raw) {
        if (raw == null) return null;
        String url = raw.trim();
        if (url.length() == 0) return null;
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            url = "https://" + url;
        }
        Uri uri = Uri.parse(url);
        if (uri.getHost() == null) return null;
        return url;
    }

    /* ==================== 生命周期与返回键 ==================== */

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode != KeyEvent.KEYCODE_BACK) return super.onKeyDown(keyCode, event);

        /* 覆盖层（错误页 / 设置页）没有网页历史可退，直接交给系统退出 */
        if (errorView != null || settingsView != null || web == null) {
            return super.onKeyDown(keyCode, event);
        }
        if (web.canGoBack()) {
            web.goBack();
            return true;
        }
        long now = System.currentTimeMillis();
        if (now - lastBackPressedAt < 2000) return super.onKeyDown(keyCode, event);
        lastBackPressedAt = now;
        Toast.makeText(this, "再按一次退出", Toast.LENGTH_SHORT).show();
        return true;
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.setWebChromeClient(null);
            web.setWebViewClient(null);
            if (stage != null) stage.removeView(web);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    private int dp(float value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
