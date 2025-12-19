import sys
import os
import json
import uuid
import socket
import threading
import time
from datetime import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QTextBrowser, QTextEdit, QPushButton, 
                               QListWidget, QListWidgetItem, QLabel, QFileDialog, 
                               QMessageBox, QDialog, QLineEdit)
from PySide6.QtCore import (Qt, QThread, Signal, QObject, QTimer, QUrl)
from PySide6.QtGui import (QDesktopServices, QBrush, QColor, QTextCharFormat, QTextCursor)

# --- 基础配置 ---
UDP_PORT = 48395
TCP_PORT = 44444
BROADCAST_IP = '<broadcast>'
CHUNK_SIZE = 16384
IMG_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff')
HEARTBEAT_INTERVAL = 5  # 发送心跳间隔(秒)
TIMEOUT_THRESHOLD = 30  # 判定离线阈值(秒)

def get_local_ip():
    try:
        # 获取所有网卡的 IP 地址
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
        # 优先选 192.168 开头的（真正的局域网地址）
        for addr in addrs:
            if addr.startswith("192.168."): return addr
        # 回退逻辑...
        return addrs[0]
    except:
        return '127.0.0.1'

# --- 自定义输入框 ---
class ChatInput(QTextEdit):
    sig_send = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.sig_send.emit()
                event.accept() 
        else:
            super().keyPressEvent(event)

# --- TCP 文件传输服务 (修复 QThread 退出问题) ---
class FileServer(QThread):
    def __init__(self):
        super().__init__()
        self.serving_files = {} 
        self.running = True
        self.server_socket = None

    def run(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('0.0.0.0', TCP_PORT))
            self.server_socket.listen(20)
            
            # 设置非阻塞或超时，以便能轮询 self.running 状态
            self.server_socket.settimeout(0.5)
            
            while self.running:
                try:
                    client, addr = self.server_socket.accept()
                    threading.Thread(target=self.handle_request, args=(client,), daemon=True).start()
                except socket.timeout:
                    continue # 超时意味着没连接，继续检查 self.running
                except:
                    break
        except Exception as e:
            print(f"FileServer Error: {e}")
        finally:
            if self.server_socket:
                try: self.server_socket.close()
                except: pass

    def handle_request(self, client):
        try:
            req = client.recv(1024).decode('utf-8')
            if req in self.serving_files:
                path = self.serving_files[req]
                if os.path.exists(path):
                    with open(path, 'rb') as f:
                        while chunk := f.read(CHUNK_SIZE):
                            client.sendall(chunk)
        except: pass
        finally: client.close()

    def stop(self):
        self.running = False
        # 必须调用 wait() 等待线程循环结束，否则会报 QThread Destroyed 错误
        self.wait()

# --- P2P 通讯引擎 ---
class P2PEngine(QObject):
    sig_status = Signal(dict)
    sig_msg = Signal(dict)
    sig_diag = Signal(dict)
    sig_downloaded = Signal(str, str, bool)
    sig_read_ack = Signal(dict)

    def __init__(self, username):
        super().__init__()
        self.username = username
        self.my_uuid = str(uuid.uuid4())
        self.my_ip = get_local_ip()
        self.msg_cache = set()
        self.running = True
        
        # 启动 TCP 文件服务器
        self.file_server = FileServer()
        self.file_server.start()
        
        # 启动 UDP socket
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.udp_sock.bind(('', UDP_PORT))
        
        # 接收线程
        self.recv_thread = threading.Thread(target=self.receive_loop, daemon=True)
        self.recv_thread.start()

        # 心跳线程
        self.hb_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.hb_thread.start()

    def send_packet(self, pkg, target_ip=BROADCAST_IP):
        pkg.update({
            "user": self.username, 
            "uuid": self.my_uuid, 
            "ip": self.my_ip,
            "msg_id": str(uuid.uuid4()), 
            "time": datetime.now().strftime("%H:%M:%S")
        })
        try:
            self.udp_sock.sendto(json.dumps(pkg).encode('utf-8'), (target_ip, UDP_PORT))
            return pkg["msg_id"]
        except: return None

    def receive_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(65535)
                pkg = json.loads(data.decode('utf-8'))
                
                # 过滤自己发的消息
                if pkg['uuid'] == self.my_uuid: continue
                
                t = pkg.get('type')
                mid = pkg.get('msg_id')

                # 消息去重
                if mid and mid in self.msg_cache: continue
                if mid: self.msg_cache.add(mid)

                # 分发信号
                if t == "status": 
                    self.sig_status.emit(pkg)
                elif t in ["text", "file"]: 
                    self.sig_msg.emit(pkg)
                elif t == "diag_ping": 
                    self.send_packet({"type": "diag_pong"}, addr[0])
                elif t == "diag_pong": 
                    self.sig_diag.emit(pkg)
                elif t == "read_ack":
                    self.sig_read_ack.emit(pkg)

            except (socket.error, json.JSONDecodeError): 
                if not self.running: break
            except Exception as e:
                print(f"UDP Recv Error: {e}")

    def heartbeat_loop(self):
        while self.running:
            self.send_packet({"type": "status", "action": "on"})
            time.sleep(HEARTBEAT_INTERVAL)

    def download_worker(self, ip, filename, save_path, sender_id, is_group):
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(10)
            client.connect((ip, TCP_PORT))
            client.send(filename.encode('utf-8'))
            
            with open(save_path, 'wb') as f:
                while True:
                    chunk = client.recv(CHUNK_SIZE)
                    if not chunk: break
                    f.write(chunk)
            
            self.sig_downloaded.emit(save_path, sender_id, is_group)
        except Exception as e:
            print(f"Download failed: {e}")
        finally: 
            try: client.close()
            except: pass

    def shutdown(self):
        self.running = False
        # 1. 停止 UDP 接收 (关闭socket以触发异常退出recvfrom)
        try: self.udp_sock.close()
        except: pass
        
        # 2. 停止文件服务器 (QThread 需要 wait)
        self.file_server.stop()
        
        # 3. 停止心跳等普通线程 (Daemon线程通常随主程序退出，但join更保险)
        if self.hb_thread.is_alive():
            # 这里简单等待一下，不做强制 join 以免界面卡顿，因为是 daemon 线程
            pass 

# --- 主界面 ---
MODERN_STYLE = """
QMainWindow { background-color: #f5f5f7; }
QWidget#Sidebar { background-color: #2c3e50; border-right: 1px solid #dcdde1; }
QLabel#MeTitle { color: #ecf0f1; font-size: 16px; font-weight: bold; padding: 10px; }
QListWidget { background-color: transparent; border: none; outline: none; }
QListWidget::item { background-color: transparent; color: #bdc3c7; padding: 12px; margin: 4px 8px; border-radius: 8px; }
QListWidget::item:selected { background-color: #34495e; color: white; }
QListWidget::item:hover { background-color: #3d566e; }
QWidget#ChatArea { background-color: white; }
QTextBrowser { background-color: white; border: none; padding: 10px; font-size: 14px; color: #2f3640; }
ChatInput { background-color: #f1f2f6; border: 2px solid #f1f2f6; border-radius: 12px; padding: 8px; font-size: 14px; margin: 10px; }
ChatInput:focus { border: 2px solid #3498db; }
QPushButton { background-color: #3498db; color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: bold; }
QPushButton:hover { background-color: #2980b9; }
QPushButton#ActionBtn { background-color: #ecf0f1; color: #2c3e50; margin: 5px; }
QPushButton#ActionBtn:hover { background-color: #dcdde1; }
"""

# 文本片段常量（统一替换）
UNREAD_SPAN = "<span style='font-size:9pt; color:gray;'>(未读)</span>"
READ_SPAN = "<span style='font-size:9pt; color:green;'>(已读)</span>"

class MainWindow(QMainWindow):
    def __init__(self, username):
        super().__init__()
        self.engine = P2PEngine(username)
        self.online_users = {} # {uuid: {ip, name, item, flash, last_seen}}
        self.chat_history = {"All": []}
        self.current_id = "All"
        self.all_flash = False

        # 记录每个会话最后一条带状态（未读/已读）的自己发送消息在 chat_history 列表中的索引
        # 结构: { chat_id: index }
        self.last_out_index = {}
        
        self.resize(1000, 750)
        
        # --- 窗口标题设置 (已恢复原来格式) ---
        self.setWindowTitle(f"局域网聊天 - {username}|") 
        
        self.init_ui()
        self.setStyleSheet(MODERN_STYLE)
        
        # 信号连接
        self.engine.sig_status.connect(self.on_status)
        self.engine.sig_msg.connect(self.on_msg)
        self.engine.sig_diag.connect(self.on_diag_reply)
        self.engine.sig_downloaded.connect(self.on_file_ready)
        self.engine.sig_read_ack.connect(self.on_read_ack_received)
        
        # 闪烁定时器
        self.flash_timer = QTimer()
        self.flash_timer.timeout.connect(self.handle_flash)
        self.flash_timer.start(500)

        # 超时检测定时器
        self.check_timer = QTimer()
        self.check_timer.timeout.connect(self.check_timeout)
        self.check_timer.start(5000)
        
        # 发送上线广播
        self.engine.send_packet({"type": "status", "action": "on"})
        self.user_list.setCurrentItem(self.item_all)
        self.switch_chat(self.item_all)
        self.flash_toggle = False

    def init_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QHBoxLayout(cw)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # 左侧
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(260)
        side_layout = QVBoxLayout(sidebar)
        
        self.lbl_me = QLabel(f"{self.engine.username} (在线)")
        self.lbl_me.setObjectName("MeTitle")
        
        self.user_list = QListWidget()
        self.item_all = QListWidgetItem("📢  公共频道 (All)")
        # 设置初始前景色，确保闪烁时可以正确切换
        self.item_all.setForeground(QBrush(QColor("#bdc3c7")))
        self.user_list.addItem(self.item_all)
        self.user_list.setCurrentItem(self.item_all)
        self.user_list.itemClicked.connect(self.switch_chat)
        
        btn_diag = QPushButton("网络诊断")
        btn_diag.setObjectName("ActionBtn")
        btn_diag.clicked.connect(self.start_diag)
        
        side_layout.addWidget(self.lbl_me)
        side_layout.addWidget(self.user_list)
        side_layout.addWidget(btn_diag)
        
        # 右侧
        chat_area = QWidget()
        chat_area.setObjectName("ChatArea")
        chat_layout = QVBoxLayout(chat_area)
        
        self.lbl_title = QLabel("公共频道")
        self.lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; padding: 15px; border-bottom: 1px solid #f1f2f6;")
        
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self.on_link_clicked)
        
        input_container = QVBoxLayout()
        self.input_box = ChatInput()
        self.input_box.setPlaceholderText("在此输入消息...")
        self.input_box.sig_send.connect(self.send_text)
        
        bottom_btns = QHBoxLayout()
        btn_file = QPushButton("📁 发送文件")
        btn_file.setObjectName("ActionBtn")
        btn_file.clicked.connect(self.send_file_dialog)
        
        btn_send = QPushButton("发送 (Enter)")
        btn_send.setFixedSize(120, 35)
        btn_send.clicked.connect(self.send_text)
        
        bottom_btns.addWidget(btn_file)
        bottom_btns.addStretch()
        bottom_btns.addWidget(btn_send)
        
        input_container.addWidget(self.input_box)
        input_container.addLayout(bottom_btns)
        input_container.setContentsMargins(10, 0, 10, 10)
        
        chat_layout.addWidget(self.lbl_title)
        chat_layout.addWidget(self.browser)
        chat_layout.addLayout(input_container)
        
        layout.addWidget(sidebar)
        layout.addWidget(chat_area)

    def add_to_history(self, chat_id, html, is_mine=False):
        if chat_id not in self.chat_history: 
            self.chat_history[chat_id] = []
        # 如果是自己发出的消息（私聊场景），保证只有最后一条保留状态标签
        if is_mine:
            # 移除之前的状态标签（如果存在），旧消息不显示任何状态
            prev_idx = self.last_out_index.get(chat_id)
            if prev_idx is not None and prev_idx < len(self.chat_history.get(chat_id, [])):
                prev_html = self.chat_history[chat_id][prev_idx]['html']
                prev_html = prev_html.replace(UNREAD_SPAN, "").replace(READ_SPAN, "")
                self.chat_history[chat_id][prev_idx]['html'] = prev_html
            # 添加新的发送消息并记录索引（该消息为当前会话的“最后发送消息”）
            self.chat_history[chat_id].append({"html": html, "is_mine": is_mine})
            self.last_out_index[chat_id] = len(self.chat_history[chat_id]) - 1
            # 如果当前正显示该会话，刷新整个浏览器以保证样式同步
            if self.current_id == chat_id:
                self.refresh_browser()
        else:
            # 如果是对方发来的消息，则需要隐藏本会话上（若存在）最后一条我们发出的状态显示
            prev_idx = self.last_out_index.get(chat_id)
            if prev_idx is not None and prev_idx < len(self.chat_history.get(chat_id, [])):
                ph = self.chat_history[chat_id][prev_idx]['html']
                ph = ph.replace(UNREAD_SPAN, "").replace(READ_SPAN, "")
                self.chat_history[chat_id][prev_idx]['html'] = ph
                # 收到对方消息后状态标签应被隐藏（不再保留 last_out_index）
                self.last_out_index.pop(chat_id, None)
                if self.current_id == chat_id:
                    self.refresh_browser()
            # 添加对方消息
            self.chat_history[chat_id].append({"html": html, "is_mine": is_mine})
            if self.current_id == chat_id:
                # 若当前会话被打开，直接追加显示并滚到底部
                cursor = self.browser.textCursor()
                cursor.movePosition(QTextCursor.End)
                if self.browser.toPlainText(): cursor.insertBlock()
                cursor.setCharFormat(QTextCharFormat())
                cursor.insertHtml(f"<div style='margin:0;'>{html}</div>")
                QTimer.singleShot(0, lambda: self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum()))
            return

        # 对于自己发出的消息且当前不是在该会话时，不自动滚动/刷新界面（上面已在 is_mine 支持 refresh_browser）
        if self.current_id == chat_id:
            QTimer.singleShot(0, lambda: self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum()))

    def on_link_clicked(self, url):
        raw_url = url.toString()
        qurl = QUrl(raw_url)
        path = qurl.toLocalFile()

        query_params = qurl.query()
        ip_address = None
        if "ip=" in query_params:
            ip_address = query_params.split("ip=")[-1]

        if os.path.exists(path):
            if path.lower().endswith(IMG_EXTS):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            else:
                folder = os.path.dirname(path)
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif ip_address:
            threading.Thread(target=self.engine.download_worker,
                            args=(ip_address, os.path.basename(path), path, "any", False),
                            daemon=True).start()
        else:
            QMessageBox.warning(self, "错误", f"文件不存在且无来源IP: {path}")

    def switch_chat(self, item):
        self.current_id = "All" if item == self.item_all else item.data(Qt.UserRole)
        
        if self.current_id == "All":
            self.all_flash = False
            self.item_all.setBackground(QBrush(QColor("transparent")))
            self.item_all.setForeground(QBrush(QColor("#bdc3c7")))
        elif self.current_id in self.online_users:
            self.online_users[self.current_id]['flash'] = False
            self.online_users[self.current_id]['item'].setBackground(QBrush(QColor("transparent")))
            self.online_users[self.current_id]['item'].setForeground(QBrush(QColor("#bdc3c7")))
            self.engine.send_packet({"type": "read_ack"}, self.online_users[self.current_id]['ip'])
        app_name = f"局域网聊天软件 - {self.engine.username}"
        quote = "独乐乐不如众乐乐" if self.current_id == "All" else "君子之交淡如水,小人之交甘若霖"
        self.setWindowTitle(f"{app_name} | {quote}")
        
        name = item.text()
        self.lbl_title.setText(f"<h2>{name}</h2>")
        self.refresh_browser()

    def refresh_browser(self):
        self.browser.clear()
        for m_obj in self.chat_history.get(self.current_id, []):
            cursor = self.browser.textCursor()
            cursor.movePosition(QTextCursor.End)
            if self.browser.toPlainText(): cursor.insertBlock()
            cursor.setCharFormat(QTextCharFormat())
            cursor.insertHtml(f"<div style='margin:0;'>{m_obj['html']}</div>")
        # 确保在 UI 更新后滚动到底部
        QTimer.singleShot(0, lambda: self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum()))

    def send_text(self):
        txt = self.input_box.toPlainText().strip()
        if not txt: return
        
        is_all = (self.current_id == "All")
        target_ip = BROADCAST_IP if is_all else self.online_users[self.current_id]['ip']
        
        # 发送私聊时在消息里加入未读标记（群聊不需要）
        t = datetime.now().strftime('%H:%M:%S')
        status = "" if is_all else UNREAD_SPAN
        html = f"<table width='100%'><tr><td>[{t}] <span style='color:#2980b9;'>我</span>：{txt}</td><td align='right'>{status}</td></tr></table>"
        
        self.engine.send_packet({"type": "text", "content": txt, "is_group": is_all}, target_ip)
        self.add_to_history(self.current_id, html, is_mine=True)
        self.input_box.clear()

    def send_file_dialog(self):
        fp, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if not fp: return
        
        fn = os.path.basename(fp)
        abs_fp = os.path.abspath(fp)
        
        self.engine.file_server.serving_files[fn] = fp
        
        is_all = (self.current_id == "All")
        ip = BROADCAST_IP if is_all else self.online_users[self.current_id]['ip']
        
        t = datetime.now().strftime('%H:%M:%S')
        status = "" if is_all else UNREAD_SPAN

        if fn.lower().endswith(IMG_EXTS):
            content = f"发送图片 {fn}<br><a href='file:///{abs_fp}'><img src='file:///{abs_fp}' width='200'></a>"
        else:
            content = f"发送文件：<a href='file:///{abs_fp}?ip={ip}'>{fn}</a>"

        html = f"<table width='100%'><tr><td>[{t}] <span style='color:#2980b9;'>我</span>：{content}</td><td align='right'>{status}</td></tr></table>"

        self.engine.send_packet({"type": "file", "filename": fn, "is_group": is_all}, ip)
        self.add_to_history(self.current_id, html, is_mine=True)

    def on_msg(self, pkg):
        uid = pkg['uuid']
        is_all = pkg.get('is_group', True)
        dest = "All" if is_all else uid
        
        if uid in self.online_users:
            self.online_users[uid]['last_seen'] = datetime.now()
        
        if not is_all and self.current_id == uid:
            self.engine.send_packet({"type": "read_ack"}, pkg['ip'])

        if pkg['type'] == "file":
            fn = pkg['filename']
            save_path = os.path.abspath(os.path.join("data", dest, fn))
            
            if fn.lower().endswith(IMG_EXTS) and not os.path.exists(save_path):
                 threading.Thread(target=self.engine.download_worker, 
                                 args=(pkg['ip'], fn, save_path, uid, is_all), 
                                 daemon=True).start()
            
            msg = f"<div>[{pkg['time']}] {pkg['user']}：发送文件 <a href='file:///{save_path}?ip={pkg['ip']}'>{fn}</a></div>"
        else:
            msg = f"[{pkg['time']}] {pkg['user']}：{pkg['content']}"
        
        # 当收到对方消息时，按需求需要隐藏（移除）本会话最后一条发送消息的状态显示
        if not is_all:
            prev_idx = self.last_out_index.get(uid)
            if prev_idx is not None and prev_idx < len(self.chat_history.get(uid, [])):
                ph = self.chat_history[uid][prev_idx]['html']
                ph = ph.replace(UNREAD_SPAN, "").replace(READ_SPAN, "")
                self.chat_history[uid][prev_idx]['html'] = ph
                self.last_out_index.pop(uid, None)
                if self.current_id == uid:
                    self.refresh_browser()
        
        self.add_to_history(dest, msg)
        
        if is_all and self.current_id != "All":
            self.all_flash = True
        elif not is_all and self.current_id != uid:
            if uid in self.online_users: 
                self.online_users[uid]['flash'] = True

    def on_file_ready(self, path, sender_id, is_group):
        dest = "All" if is_group else sender_id
        abs_path = os.path.abspath(path)
        fn = os.path.basename(path)

        if fn.lower().endswith(IMG_EXTS):
            file_html = f"<div><p style='color:blue;'>✔ 图片已接收:</p><a href='file:///{abs_path}'><img src='file:///{abs_path}' width='200'></a></div>"
        else:
            file_html = f"<div><font color='blue'>✔ 下载完成：<a href='file:///{abs_path}'>{fn}</a></font></div>"
        
        self.add_to_history(dest, file_html)

    def on_read_ack_received(self, pkg):
        sender_uuid = pkg.get('uuid')
        if sender_uuid in self.last_out_index:
            idx = self.last_out_index[sender_uuid]
            if sender_uuid in self.chat_history and idx < len(self.chat_history[sender_uuid]):
                # 只更新最后一条发送消息为已读
                self.chat_history[sender_uuid][idx]['html'] = self.chat_history[sender_uuid][idx]['html'].replace(UNREAD_SPAN, READ_SPAN)
                # 刷新当前视图（若在该会话中）
                if self.current_id == sender_uuid:
                    self.refresh_browser()

    def on_status(self, pkg):
        uid = pkg['uuid']
        name = pkg['user']
        display_name = f"{name}({uid[-5:]})"
        t = datetime.now().strftime('%H:%M:%S')
        
        if pkg['action'] == "on":
            if uid in self.online_users:
                self.online_users[uid]['last_seen'] = datetime.now()
                self.online_users[uid]['ip'] = pkg['ip']
                return

            it = QListWidgetItem(f"👤 {display_name}")
            it.setData(Qt.UserRole, uid)
            # 设置初始前景色，确保后续闪烁能正确切换
            it.setForeground(QBrush(QColor("#bdc3c7")))
            self.user_list.addItem(it)
            self.online_users[uid] = {
                "ip": pkg['ip'], 
                "name": name, 
                "item": it, 
                "flash": False,
                "last_seen": datetime.now()
            }
            if not pkg.get('is_reply'):
                self.engine.send_packet({"type": "status", "action": "on", "is_reply": True}, pkg['ip'])
                
            self.add_to_history("All", f"[{t}] <font color='green'>{display_name} 上线</font>")
        
        elif pkg['action'] == "off":
            if uid in self.online_users:
                row = self.user_list.row(self.online_users[uid]['item'])
                self.user_list.takeItem(row)
                del self.online_users[uid]
                
                reason = pkg.get('reason', '主动退出')
                self.add_to_history("All", f"[{t}] <font color='red'>{display_name} 下线 下线原因:{reason}</font>")

    def check_timeout(self):
        now = datetime.now()
        dead_users = []
        for uid, info in self.online_users.items():
            delta = (now - info['last_seen']).total_seconds()
            if delta > TIMEOUT_THRESHOLD:
                dead_users.append(uid)
        for uid in dead_users:
            self.on_status({
                "uuid": uid, 
                "user": self.online_users[uid]['name'], 
                "action": "off", 
                "reason": "超时"
            })

    def handle_flash(self):
        # 核心逻辑：直接反转布尔值，不再判断当前颜色
        self.flash_toggle = not self.flash_toggle

        # 确定本次闪烁的颜色
        flash_color = QColor("#f39c12") if self.flash_toggle else QColor("transparent")
        text_color = QColor("#ffffff") if self.flash_toggle else QColor("#bdc3c7")

        # 1. 刷新群聊项
        if self.all_flash:
            self.item_all.setBackground(QBrush(flash_color))
            self.item_all.setForeground(QBrush(text_color))

        # 2. 遍历在线用户列表
        for uid, d in self.online_users.items():
            if d['flash']:
                d['item'].setBackground(QBrush(flash_color))
                d['item'].setForeground(QBrush(text_color))

    def start_diag(self):
        self.diag_res = {}
        for uid, info in self.online_users.items():
            self.diag_res[uid] = "超时 ❌"
            self.engine.send_packet({"type": "diag_ping"}, info['ip'])
        QTimer.singleShot(2000, self.show_diag_result)

    def show_diag_result(self):
        msg = "\n".join([f"{self.online_users[u]['name']} : {s}" for u, s in self.diag_res.items()])
        if not msg: msg = "当前没有其他用户在线"
        QMessageBox.information(self, "网络诊断结果", msg)

    def on_diag_reply(self, pkg):
        if pkg['uuid'] in self.diag_res:
            self.diag_res[pkg['uuid']] = "正常 ✅"

    def closeEvent(self, event):
        # 1. 发送下线包
        self.engine.send_packet({"type": "status", "action": "off", "reason": "主动退出"})
        # 2. 清理资源并等待线程结束
        self.engine.shutdown()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    os.makedirs("data/All", exist_ok=True)
    
    dlg = QDialog()
    dlg.setWindowTitle("局域网聊天")
    dlg.resize(300, 150)
    l = QVBoxLayout(dlg)
    l.addWidget(QLabel("输入用户名:"))
    e = QLineEdit()
    l.addWidget(e)
    b = QPushButton("登录")
    b.clicked.connect(dlg.accept)
    l.addWidget(b)
    
    if dlg.exec() == QDialog.Accepted:
        name = e.text().strip() or "User"
        win = MainWindow(name)
        win.show()
        sys.exit(app.exec())