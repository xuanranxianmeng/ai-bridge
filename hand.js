/* AI 桥 · 手机端执行体（AutoX.js）\n * 原始作者注释已保留；本副本为公开版，敏感信息已脱敏。\n * 完整项目见仓库 README。\n */
// 手机端执行体 · v6 正式版（自更新版：装完这次，以后可自行拉取新版本）// 2026-08-23
var PORT = 8789;
var KEY = '你的口令'; // ⚠️ 部署前改为自己的密钥，别把真实口令提交到仓库
var VER = 'v12';
var SELF_PATH = '/storage/emulated/0/脚本/hand.js';
var UPDATE_URL = 'http://你的服务器:9009/probe.txt // ⚠️ 换成你自己的更新源地址';
console.log(VER + ' 启动中...');

function findAndClick(matchKey, val) {
    var w = null;
    if (matchKey === 'text') w = text(val).findOnce();
    else if (matchKey === 'desc') w = desc(val).findOnce();
    else if (matchKey === 'id') w = id(val).findOnce();
    if (!w) return null;
    var b = w.bounds();
    var x = b.centerX(), y = b.centerY();
    click(x, y);
    return { ok: true, clicked: [x, y] };
}

function doAction(req) {
    var a = req.action;
    switch (a) {
        case 'ping':
            return { ok: true, msg: 'pong', ver: VER, device: String(device.brand) + ' ' + String(device.model), battery: device.getBattery() };
        case 'update': {
            var res = http.get(UPDATE_URL);
            var code = res.body.string();
            if (!code || code.length < 200) return { ok: false, err: '拉取的新代码太短(' + (code ? code.length : 0) + ')，放弃更新' };
            files.write(SELF_PATH, code);
            return { ok: true, msg: '新代码已写入 ' + SELF_PATH + ' 长度' + code.length + '，发 restart 即可生效' };
        }
        case 'restart': {
            threads.start(function () {
                sleep(600);
                engines.execScriptFile(SELF_PATH);
                sleep(2000);
                exit();
            });
            return { ok: true, msg: '重启流程已在后台启动' };
        }
        case 'toast':
            toast(req.msg); return { ok: true };
        case 'launch': {
            var r = app.launch(String(req.name));
            return { ok: !!r, err: r ? null : '找不到应用:' + req.name };
        }
        case 'launchPackage': {
            var r2 = app.launchPackage(String(req.pkg));
            return { ok: !!r2, err: r2 ? null : '找不到包:' + req.pkg };
        }
        case 'clickText': {
            var c = findAndClick(req.matchKey || req.key || 'text', req.text);
            return c || { ok: false, err: '屏幕上找不到:' + req.text };
        }
        case 'press':
            press(req.x, req.y, req.dur || 300); return { ok: true };
        case 'swipe':
            swipe(req.x1, req.y1, req.x2, req.y2, req.dur || 500); return { ok: true };
        case 'shell': {
            var cmd = String(req.cmd);
            try {
                if (typeof shizuku === 'function') {
                    var r = shizuku(cmd);
                    var o = String((r && (r.result || r.output)) || r || '');
                    return { ok: true, via: 'shizuku', out: o };
                }
            } catch (e) {}
            try {
                var o2 = String($shell(cmd).result);
                return { ok: true, via: 'shell', out: o2 };
            } catch (e) {}
            try {
                var o3 = String(sh.exec(cmd).result);
                return { ok: true, via: 'sh', out: o3 };
            } catch (e) {}
            return { ok: false, err: '无可用 shell 途径(需要 Shizuku/Root)' };
        }
        case 'shizstatus': {
            var alive = false;
            try { alive = !!shizuku.isAlive(); } catch (e) {}
            return { ok: true, shizuku: alive, hasFn: typeof shizuku };
        }
        case 'shot': {
            var p = '/sdcard/bridge_shot.png';
            try {
                if (typeof shizuku === 'function') shizuku('screencap -p ' + p);
                else $shell('screencap -p ' + p);
            } catch (e) { return { ok: false, err: '截图执行失败: ' + String(e) }; }
            try {
                var bs = files.readBytes(p);
                if (!bs) return { ok: false, err: '截图文件不存在' };
                var b64 = android.util.Base64.encodeToString(bs, android.util.Base64.NO_WRAP);
                return { ok: true, size: bs.length, b64: b64 };
            } catch (e) { return { ok: false, err: '读图失败: ' + String(e) }; }
        }
        case 'rawdump': {
            var out = [];
            try {
                var root = auto.service.getRootInActiveWindow();
                if (!root) return { ok: false, err: '无障碍树为空(root null)' };
                function walk(node) {
                    if (!node || out.length >= 400) return;
                    try {
                        var t = node.getText ? node.getText() : null;
                        var d = node.getContentDescription ? node.getContentDescription() : null;
                        var b = new android.graphics.Rect();
                        node.getBoundsInScreen(b);
                        out.push({
                            cls: node.getClassName() ? String(node.getClassName()) : '',
                            t: t ? String(t) : '',
                            d: d ? String(d) : '',
                            pkg: node.getPackageName() ? String(node.getPackageName()) : '',
                            x: b.centerX(), y: b.centerY()
                        });
                    } catch (e) {}
                    for (var i = 0; i < node.getChildCount() && out.length < 400; i++) {
                        walk(node.getChild(i));
                    }
                }
                walk(root);
            } catch (e) {
                return { ok: false, err: String(e) };
            }
            return { ok: true, n: out.length, nodes: out };
        }
        case 'nodes': {
            var out = [];
            function pushNode(w, k) {
                try {
                    var b = w.bounds();
                    out.push({ k: k, v: String(w[k]()), x: b.centerX(), y: b.centerY(), w: b.width(), h: b.height() });
                } catch (e) {}
            }
            try {
                var ts = textMatches('.+').find();
                for (var i = 0; i < ts.length; i++) pushNode(ts[i], 'text');
            } catch (e) {}
            try {
                var ds = descMatches('.+').find();
                for (var j = 0; j < ds.length; j++) pushNode(ds[j], 'desc');
            } catch (e) {}
            return { ok: true, n: out.length, nodes: out.slice(0, 300) };
        }
        case 'screenshot': {
            if (!requestScreenCapture()) return { ok: false, err: '截屏授权没通过' };
            sleep(400);
            var img = captureScreen();
            var b64 = images.toBase64(img, 'jpg', 55);
            return { ok: true, ver: VER, w: img.getWidth(), h: img.getHeight(), b64: b64 };
        }
        case 'back': back(); return { ok: true };
        case 'home': home(); return { ok: true };
        case 'setClip':
            setClip(String(req.text)); return { ok: true };
        case 'current':
            return { ok: true, pkg: String(currentPackage()), act: String(currentActivity()) };
        case 'clickEdit': {
            var et = className('android.widget.EditText').findOnce();
            if (!et) return { ok: false, err: '找不到输入框' };
            var eb = et.bounds();
            click(eb.centerX(), eb.centerY());
            return { ok: true, clicked: [eb.centerX(), eb.centerY()] };
        }
        case 'input': {
            var box = focused(true).findOnce() || className('android.widget.EditText').findOnce();
            if (!box) return { ok: false, err: '找不到聚焦输入框' };
            try {
                if (box.setText && box.setText(String(req.text))) return { ok: true, how: 'setText' };
            } catch (e1) {}
            setClip(String(req.text));
            sleep(300);
            try {
                if (box.paste && box.paste()) return { ok: true, how: 'paste' };
            } catch (e2) {}
            return { ok: false, err: '两种输入方式都失败了' };
        }
        default:
            return { ok: false, err: '未知指令:' + a };
    }
}

function handle(sock) {
    try {
        var input = sock.getInputStream();
        var buf = java.lang.reflect.Array.newInstance(java.lang.Byte.TYPE, 8192);
        var data = new java.io.ByteArrayOutputStream();
        var n, contentLength = 0;
        while ((n = input.read(buf)) != -1) {
            data.write(buf, 0, n);
            var all = data.toByteArray();
            var head = new java.lang.String(all, 'UTF-8');
            var p = head.indexOf('\r\n\r\n');
            if (p >= 0) {
                var m = /Content-Length:\s*(\d+)/i.exec(head);
                contentLength = m ? parseInt(m[1]) : 0;
                if (all.length >= p + 4 + contentLength) break;
            }
        }
        var all = data.toByteArray();
        var full = new java.lang.String(all, 'UTF-8');
        var pp = full.indexOf('\r\n\r\n');
        var body = pp >= 0 ? full.substring(pp + 4) : '';
        var respText = JSON.stringify({ ok: true, msg: 'bridge-hand v4 alive' });
        if (body) {
            try {
                var req = JSON.parse(body);
                if ((req.auth || req.key) !== KEY) throw new Error('密钥不对~');
                respText = JSON.stringify(doAction(req));
                console.log('执行:' + req.action + ' -> ' + respText.slice(0, 120));
            } catch (e) {
                respText = JSON.stringify({ ok: false, err: String(e && e.message || e) });
            }
        }
        var bytes = new java.lang.String(respText).getBytes('UTF-8');
        var out = sock.getOutputStream();
        out.write(new java.lang.String('HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\nContent-Length: ' + bytes.length + '\r\n\r\n').getBytes('UTF-8'));
        out.write(bytes);
        out.flush();
    } catch (e) {
        console.error(e);
    } finally {
        try { sock.close(); } catch (e2) {}
    }
}

threads.start(function () {
    var server = null;
    for (var i = 0; i < 15; i++) {
        try { server = new java.net.ServerSocket(PORT, 50); break; }
        catch (e) { console.log('端口被占，等旧实例退场...(' + (i + 1) + '/15)'); sleep(1000); }
    }
    if (!server) { console.error('抢端口失败，退出'); exit(); return; }
    console.log('手脚已就位，监听 ' + PORT);
    while (true) {
        var s = server.accept();
        threads.start((function (sk) { return function () { handle(sk); }; })(s));
    }
});

var nis = java.net.NetworkInterface.getNetworkInterfaces();
while (nis.hasMoreElements()) {
    var ni = nis.nextElement();
    var addrs = ni.getInetAddresses();
    while (addrs.hasMoreElements()) {
        var a = addrs.nextElement();
        if (!a.isLoopbackAddress() && a.getHostAddress().indexOf(':') < 0) {
            console.log('我的地址: ' + a.getHostAddress());
        }
    }
}
console.log('v4 就绪，主线程待机（保持运行别关）');
while (true) { sleep(60000); }


