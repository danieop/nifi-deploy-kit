#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAIRN / UNC-CAIRN (202.61.139.15) - APACHE NIFI FORENSIC & BACKDOOR HUNTER
Môi trường: Python 3.6+ (Zero external dependencies - Chỉ dùng Standard Library)
Chức năng: Rà soát chuyên sâu flow.json.gz/flow.xml.gz (kể cả archive),
           kiểm tra Ghost Processor, JDBC RCE, NAR backdoor, NiFi Logs và OS Persistence.
"""

import os
import sys
import gzip
import json
import re
import glob
import time
import argparse
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ==============================================================================
# 1. ĐỊNH NGHĨA BỘ CHỈ SỐ XÂM NHẬP (IOCs) CHIẾN DỊCH CAIRN & NIFI RCE
# ==============================================================================
CAIRN_IPS = [
    "202.61.139.15", "202.61.139.137",  # Primary & Secondary C2/Staging
    "129.204.198.254", "193.233.91.227", "217.160.132.82",
    "45.77.177.19", "46.38.232.153", "5.45.100.28", "5.45.101.64",
    "52.128.241.98", "61.91.159.242", "89.116.31.49", "97.68.27.90",
    # Operator-side IPs
    "20.78.168.45", "20.214.237.197", "139.180.159.213",
    "8.212.152.211", "42.78.192.163", "102.0.27.92"
]

CAIRN_SSH_KEY_FRAGMENT = "AAAAC3NzaC1lZDI1NTE5AAAAIKo+sIL3HUlVhjn6PDSGak+C/hEMAx+xL63z8ZhE9tHa"
CAIRN_SSH_COMMENT = "us@us-VMware-Virtual-Platform"

CAIRN_ARTIFACTS = [
    "cred_scan.py", "config_cred_scan.py", "i051_shock_rce.py",
    "MssqlProbe.java", "sakip_i061.tgz", "cairn_guard.sh", "cairn_disk_guard.sh",
    "resone-shinad.com", "sdflhl45789djj4g.cyou", "khnnversdf.top"
]

# Các Processor thường bị lợi dụng để lấy RCE hoặc đẩy dữ liệu ra ngoài (Exfiltration)
DANGEROUS_PROCESSORS = [
    "ExecuteProcess", "ExecuteStreamCommand", "ExecuteScript",
    "InvokeScriptedProcessor", "ExecuteGroovyScript", "ExecuteSQL",
    "PutHTTP", "InvokeHTTP", "PostHTTP", "ListenHTTP", "ListenTCP"
]

# Từ khóa nghi vấn trong cấu hình Processor / Script Body / Command Arguments
SUSPICIOUS_CMD_PATTERNS = re.compile(
    r"(curl\s+|wget\s+|bash\s+-i|/dev/tcp/|python3?\s+-c|socat\s+|nc\s+-e|"
    r"mkfifo|base64\s+-d|chmod\s+\+x|/tmp/|http\.server|:9000|:4445|:4446|"
    r":8896|:8899|:8997|:9099|:9201|:9955|PUT\s+/ev/|java\.lang\.Runtime|"
    r"ProcessBuilder|INIT=RUNSCRIPT|javax\.naming\.InitialContext)",
    re.IGNORECASE
)

# Ngưỡng tọa độ phát hiện Ghost Processor (ẩn ngoài vùng nhìn thấy của Canvas)
GHOST_COORD_THRESHOLD = 15000.0

# ==============================================================================
# 2. LỚP GHI NHẬN KẾT QUẢ & ĐỊNH DẠNG BÁO CÁO
# ==============================================================================
class ForensicReporter:
    def __init__(self, json_output_path=None):
        self.findings = []
        self.json_output_path = json_output_path

    def log(self, severity, category, title, details):
         colors = {
            "CRITICAL": "\033[91m[CRITICAL]\033[0m",
            "HIGH":     "\033[93m[HIGH]    \033[0m",
            "MEDIUM":   "\033[94m[MEDIUM]  \033[0m",
            "INFO":     "\033[92m[INFO]    \033[0m"
        }
        prefix = colors.get(severity, f"[{severity}]")
        print(f"{prefix} [{category}] {title}")
        if details:
            for k, v in details.items():
                print(f"    └── {k}: {v}")
        self.findings.append({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "severity": severity,
            "category": category,
            "title": title,
            "details": details
        })

    def export_summary(self):
        print("\n" + "=" * 78)
        crit = sum(1 for f in self.findings if f["severity"] == "CRITICAL")
        high = sum(1 for f in self.findings if f["severity"] == "HIGH")
        med  = sum(1 for f in self.findings if f["severity"] == "MEDIUM")
        print(f" TỔNG KẾT RÀ SOÁT: CRITICAL: {crit} | HIGH: {high} | MEDIUM: {med}")
        print("=" * 78)

        if self.json_output_path:
            try:
                with open(self.json_output_path, "w", encoding="utf-8") as f:
                    json.dump(self.findings, f, indent=2, ensure_ascii=False)
                print(f"[+] Đã xuất báo cáo JSON chi tiết tại: {self.json_output_path}")
            except Exception as e:
                print(f"[-] Không thể ghi tệp báo cáo JSON: {e}")

# ==============================================================================
# 3. MODULE 1: PHÂN TÍCH CẤU HÌNH LUỒNG (flow.json.gz & flow.xml.gz)
# ==============================================================================
def inspect_processor_dict(proc, source_file, reporter):
    proc_id   = proc.get("identifier", proc.get("id", "UNKNOWN"))
    proc_name = proc.get("name", "UNNAMED")
    proc_type = proc.get("type", "UNKNOWN_TYPE")
    short_type = proc_type.split(".")[-1]
    pos       = proc.get("position", {})
    pos_x     = float(pos.get("x", 0.0)) if pos and "x" in pos else 0.0
    pos_y     = float(pos.get("y", 0.0)) if pos and "y" in pos else 0.0
    props     = proc.get("properties", {}) or {}
    sched     = proc.get("schedulingStrategy", "UNKNOWN")
    period    = proc.get("schedulingPeriod", "UNKNOWN")
    state     = proc.get("scheduledState", "UNKNOWN")

    # 1. Kiểm tra tọa độ bất thường (Ghost Processor Backdoor)
    is_ghost = abs(pos_x) > GHOST_COORD_THRESHOLD or abs(pos_y) > GHOST_COORD_THRESHOLD

    # 2. Gộp toàn bộ chuỗi cấu hình properties để quét IOC & câu lệnh đáng ngờ
    props_str = json.dumps(props, ensure_ascii=False)
    matched_iocs = [ip for ip in CAIRN_IPS if ip in props_str]
    matched_artifacts = [art for art in CAIRN_ARTIFACTS if art.lower() in props_str.lower()]
    cmd_match = SUSPICIOUS_CMD_PATTERNS.search(props_str)

    # Đánh giá mức độ nghiêm trọng
    if matched_iocs or matched_artifacts:
        reporter.log("CRITICAL", "FLOW_CONFIG", f"Phát hiện IOC chiến dịch CAIRN trong Processor '{proc_name}'", {
            "File": source_file,
            "Processor ID": proc_id,
            "Type": proc_type,
            "Position": f"({pos_x}, {pos_y})",
            "State": f"{state} ({sched}: {period})",
            "Matched IOCs": matched_iocs + matched_artifacts,
            "Properties": props_str[:500]
        })
    elif any(dp.lower() in short_type.lower() for dp in DANGEROUS_PROCESSORS):
        sev = "CRITICAL" if (is_ghost or cmd_match) else "HIGH"
        reason = []
        if is_ghost:
            reason.append(f"Tọa độ ẩn ngoài Canvas ({pos_x}, {pos_y})")
        if cmd_match:
            reason.append(f"Chứa mẫu lệnh nguy hiểm '{cmd_match.group(0)}'")
        if not reason:
            reason.append("Processor thuộc nhóm có khả năng thực thi lệnh / kết nối ngoài")

        reporter.log(sev, "FLOW_CONFIG", f"Processor nhạy cảm: {short_type} ('{proc_name}')", {
            "File": source_file,
            "Processor ID": proc_id,
            "Reason": "; ".join(reason),
            "Position": f"({pos_x}, {pos_y})",
            "State": f"{state} ({sched}: {period})",
            "Properties": props_str[:500]
        })
    elif is_ghost:
        reporter.log("MEDIUM", "FLOW_CONFIG", f"Processor đặt tại tọa độ cực xa (nghi vấn ẩn giấu): '{proc_name}'", {
            "File": source_file,
            "Processor ID": proc_id,
            "Type": short_type,
            "Position": f"({pos_x}, {pos_y})"
        })

def inspect_controller_service_dict(cs, source_file, reporter):
    cs_id   = cs.get("identifier", cs.get("id", "UNKNOWN"))
    cs_name = cs.get("name", "UNNAMED")
    cs_type = cs.get("type", "UNKNOWN_TYPE")
    props   = cs.get("properties", {}) or {}
    props_str = json.dumps(props, ensure_ascii=False)

    # Kiểm tra dấu hiệu CVE-2023-34468 (H2 JDBC RCE) hoặc nạp Custom JAR/Driver lạ
    if re.search(r"(jdbc:h2:.*INIT=RUNSCRIPT|jdbc:.*socketFactory|/tmp/.*\.jar|http://|https://)", props_str, re.IGNORECASE):
        reporter.log("CRITICAL", "FLOW_CONTROLLER_SVC", f"Controller Service có dấu hiệu RCE / Nạp Driver độc hại: '{cs_name}'", {
            "File": source_file,
            "Service ID": cs_id,
            "Type": cs_type,
            "Properties": props_str[:500]
        })

def walk_json_flow(node, source_file, reporter):
    if isinstance(node, dict):
        comp_type = node.get("componentType", "")
        if comp_type == "PROCESSOR" or ("type" in node and "processors" in str(node.get("type", "")).lower()):
            inspect_processor_dict(node, source_file, reporter)
        elif comp_type == "CONTROLLER_SERVICE":
            inspect_controller_service_dict(node, source_file, reporter)

        for k, v in node.items():
            if k == "processors" and isinstance(v, list):
                for p in v:
                    if isinstance(p, dict):
                        inspect_processor_dict(p, source_file, reporter)
            elif k == "controllerServices" and isinstance(v, list):
                for cs in v:
                    if isinstance(cs, dict):
                        inspect_controller_service_dict(cs, source_file, reporter)
            else:
                walk_json_flow(v, source_file, reporter)
    elif isinstance(node, list):
        for item in node:
            walk_json_flow(item, source_file, reporter)

def parse_xml_flow(xml_bytes, source_file, reporter):
    try:
        root = ET.fromstring(xml_bytes)
        for proc in root.iter("processor"):
            p_dict = {
                "id": proc.findtext("id", "UNKNOWN"),
                "name": proc.findtext("name", "UNNAMED"),
                "type": proc.findtext("class", "UNKNOWN_TYPE"),
                "schedulingStrategy": proc.findtext("schedulingStrategy", ""),
                "schedulingPeriod": proc.findtext("schedulingPeriod", ""),
                "scheduledState": proc.findtext("scheduledState", ""),
                "position": {
                    "x": proc.find("position").findtext("x", "0") if proc.find("position") is not None else "0",
                    "y": proc.find("position").findtext("y", "0") if proc.find("position") is not None else "0"
                },
                "properties": {}
            }
            for prop in proc.findall("property"):
                k = prop.findtext("name", "")
                v = prop.findtext("value", "")
                if k:
                    p_dict["properties"][k] = v
            inspect_processor_dict(p_dict, source_file, reporter)
    except Exception as e:
        reporter.log("INFO", "FLOW_PARSER", f"Không thể phân tích cú pháp XML tại {source_file}", {"Error": str(e)})

def scan_flow_files(nifi_dir, scan_archives, reporter):
    search_patterns = [
        os.path.join(nifi_dir, "**", "flow.json.gz"),
        os.path.join(nifi_dir, "**", "flow.xml.gz"),
        os.path.join(nifi_dir, "**", "flow.json"),
        os.path.join(nifi_dir, "**", "flow.xml")
    ]
    flow_files = set()
    for pat in search_patterns:
        for f in glob.glob(pat, recursive=True):
            if not scan_archives and "archive" in f:
                continue
            flow_files.add(f)

    if not flow_files:
        reporter.log("INFO", "FLOW_SCAN", "Không tìm thấy tệp flow.json.gz / flow.xml.gz trong thư mục chỉ định", {"Search Path": nifi_dir})
        return

    for fpath in sorted(flow_files):
        try:
            if fpath.endswith(".gz"):
                with gzip.open(fpath, "rb") as gz:
                    raw_bytes = gz.read()
            else:
                with open(fpath, "rb") as fp:
                    raw_bytes = fp.read()

            raw_text = raw_bytes.decode("utf-8", errors="replace")

            # Quét nhanh IOC thô trên toàn bộ nội dung file flow
            for ip in CAIRN_IPS:
                if ip in raw_text:
                    reporter.log("CRITICAL", "FLOW_RAW_IOC", f"Phát hiện trực tiếp IP chiến dịch CAIRN ({ip}) trong tệp cấu hình luồng!", {"File": fpath})

            if fpath.endswith(".json.gz") or fpath.endswith(".json"):
                data = json.loads(raw_text)
                walk_json_flow(data, fpath, reporter)
            elif fpath.endswith(".xml.gz") or fpath.endswith(".xml"):
                parse_xml_flow(raw_bytes, fpath, reporter)

        except Exception as e:
            reporter.log("INFO", "FLOW_SCAN", f"Lỗi khi đọc tệp {fpath}", {"Error": str(e)})

# ==============================================================================
# 4. MODULE 2: QUÉT NHẬT KÝ NIFI (nifi-user.log, nifi-app.log, nifi-bootstrap.log)
# ==============================================================================
def scan_nifi_logs(nifi_dir, reporter):
    log_files = glob.glob(os.path.join(nifi_dir, "**", "nifi-*.log*"), recursive=True)
    if not log_files:
        reporter.log("INFO", "LOG_SCAN", "Không tìm thấy tệp log nifi-*.log trong thư mục", {"Search Path": nifi_dir})
        return

    api_mutation_regex = re.compile(
        r"(POST|PUT|DELETE)\s+/nifi-api/(process-groups/[^/]+/processors|processors/[^/\s]+|controller-services|snippets|FlowFile)",
        re.IGNORECASE
    )

    for lpath in sorted(log_files):
        try:
            open_fn = gzip.open if lpath.endswith(".gz") else open
            with open_fn(lpath, "rt", encoding="utf-8", errors="replace") as lf:
                for line_no, line in enumerate(lf, 1):
                    # 1. Quét IP C2/Operator và Artifacts của CAIRN
                    for ip in CAIRN_IPS:
                        if ip in line:
                            reporter.log("CRITICAL", "NIFI_LOG_IOC", f"Phát hiện IP chiến dịch CAIRN ({ip}) trong log!", {
                                "File": f"{lpath}:{line_no}",
                                "Log Entry": line.strip()[:400]
                            })
                    for art in CAIRN_ARTIFACTS:
                        if art.lower() in line.lower():
                            reporter.log("CRITICAL", "NIFI_LOG_IOC", f"Phát hiện công cụ CAIRN ({art}) trong log!", {
                                "File": f"{lpath}:{line_no}",
                                "Log Entry": line.strip()[:400]
                            })

                    # 2. Quét dấu hiệu User-Agent đặc trưng của Cairn Agent: pi (linux / pi (win32
                    if "pi (linux" in line.lower() or "pi (win32" in line.lower() or "PUT /ev/" in line:
                        reporter.log("CRITICAL", "NIFI_LOG_IOC", "Phát hiện User-Agent 'pi (...)' hoặc mẫu Exfiltration 'PUT /ev/' của CAIRN!", {
                            "File": f"{lpath}:{line_no}",
                            "Log Entry": line.strip()[:400]
                        })

                    # 3. Quét hành vi gọi REST API tạo/sửa/xóa Processor trong nifi-user.log (Bắt cả SSRF Proxy nội bộ)
                    if "nifi-user" in os.path.basename(lpath):
                        if api_mutation_regex.search(line) or "Anonymous" in line:
                            sev = "HIGH" if ("Anonymous" in line or "DELETE" in line or "POST" in line) else "MEDIUM"
                            reporter.log(sev, "NIFI_USER_API", "Ghi nhận thao tác gọi REST API thay đổi cấu hình / Anonymous Access", {
                                "File": f"{lpath}:{line_no}",
                                "Log Entry": line.strip()[:400]
                            })

                    # 4. Quét thực thi lệnh trong nifi-app.log
                    if "nifi-app" in os.path.basename(lpath):
                        if any(dp in line for dp in ["ExecuteProcess", "ExecuteStreamCommand", "ExecuteScript"]) and SUSPICIOUS_CMD_PATTERNS.search(line):
                            reporter.log("CRITICAL", "NIFI_APP_EXEC", "Phát hiện Processor thực thi lệnh hệ thống đáng ngờ trong nifi-app.log", {
                                "File": f"{lpath}:{line_no}",
                                "Log Entry": line.strip()[:400]
                            })
        except Exception as e:
            reporter.log("INFO", "LOG_SCAN", f"Không thể đọc tệp log {lpath}", {"Error": str(e)})

# ==============================================================================
# 5. MODULE 3: RÀ SOÁT CẤU HÌNH BẢO MẬT, CUSTOM NAR & OS PERSISTENCE
# ==============================================================================
def scan_system_and_persistence(nifi_dir, recent_days, reporter):
    # 1. Kiểm tra nifi.properties & bootstrap.conf
    for prop_file in glob.glob(os.path.join(nifi_dir, "**", "nifi.properties"), recursive=True):
        try:
            with open(prop_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                if re.search(r"nifi\.security\.allow\.anonymous\.authentication\s*=\s*true", content, re.IGNORECASE):
                    reporter.log("HIGH", "NIFI_CONFIG", "Cấu hình NiFi đang bật cho phép truy cập không xác thực (Anonymous Authentication = true)!", {
                        "File": prop_file,
                        "Risk": "Dễ dàng bị AI Agent khai thác RCE qua SSRF Proxy nội bộ"
                    })
        except Exception:
            pass

    for boot_file in glob.glob(os.path.join(nifi_dir, "**", "bootstrap.conf"), recursive=True):
        try:
            with open(boot_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip().startswith("#") and ("javaagent" in line.lower() or "/tmp/" in line.lower()):
                        reporter.log("CRITICAL", "NIFI_BOOTSTRAP", "Phát hiện tham số JVM bất thường (-javaagent hoặc đường dẫn /tmp) trong bootstrap.conf!", {
                            "File": boot_file,
                            "Config Line": line.strip()
                        })
        except Exception:
            pass

    # 2. Kiểm tra các tệp .nar / .jar mới bị chèn hoặc sửa đổi
    cutoff_time = time.time() - (recent_days * 86400)
    for ext in ("*.nar", "*.jar", "*.py", "*.sh"):
        for fpath in glob.glob(os.path.join(nifi_dir, "**", ext), recursive=True):
            if "work/nar/extensions" in fpath:
                continue  # Bỏ qua thư mục bung nén tạm của NiFi
            try:
                mtime = os.path.getmtime(fpath)
                if mtime >= cutoff_time:
                    dt_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    sev = "HIGH" if fpath.endswith((".py", ".sh")) else "MEDIUM"
                    reporter.log(sev, "FILE_INTEGRITY", f"Tệp thực thi / thư viện mới được tạo hoặc sửa đổi trong {recent_days} ngày qua", {
                        "File": fpath,
                        "Modified Time": dt_str
                    })
            except Exception:
                pass

    # 3. Quét thư mục /tmp và /var/tmp tìm công cụ của CAIRN
    for tmp_dir in ["/tmp", "/var/tmp", "/dev/shm"]:
        if os.path.exists(tmp_dir):
            for root_d, _, files in os.walk(tmp_dir):
                for fname in files:
                    full_p = os.path.join(root_d, fname)
                    if any(art.lower() in fname.lower() for art in CAIRN_ARTIFACTS) or fname.endswith((".py", ".sh", ".elf", ".tgz")):
                        reporter.log("HIGH", "TEMP_ARTIFACTS", f"Phát hiện tệp khả nghi trong thư mục tạm ({tmp_dir})", {
                            "File": full_p
                        })

    # 4. Kiểm tra SSH Authorized Keys tìm khóa của us@us-VMware-Virtual-Platform
    ssh_paths = glob.glob("/root/.ssh/authorized_keys") + glob.glob("/home/*/.ssh/authorized_keys") + glob.glob(os.path.join(nifi_dir, "**", ".ssh", "authorized_keys"), recursive=True)
    for spath in set(ssh_paths):
        try:
            with open(spath, "r", encoding="utf-8", errors="replace") as sf:
                for lno, line in enumerate(sf, 1):
                    if CAIRN_SSH_KEY_FRAGMENT in line or CAIRN_SSH_COMMENT in line:
                        reporter.log("CRITICAL", "OS_PERSISTENCE", "PHÁT HIỆN KHÓA SSH CỦA KẺ TẤN CÔNG CAIRN TRONG AUTHORIZED_KEYS!", {
                            "File": f"{spath}:{lno}",
                            "Key Entry": line.strip()
                        })
        except Exception:
            pass

    # 5. Kiểm tra tiến trình con của NiFi và kết nối mạng đang mở
    try:
        ps_out = subprocess.check_output(["ps", "-ef"], universal_newlines=True, stderr=subprocess.DEVNULL)
        for line in ps_out.splitlines():
            if any(ip in line for ip in CAIRN_IPS) or any(art in line for art in CAIRN_ARTIFACTS):
                reporter.log("CRITICAL", "ACTIVE_PROCESS", "Phát hiện tiến trình đang chạy chứa IOC của chiến dịch CAIRN!", {
                    "Process": line.strip()
                })
            elif re.search(r"(python3?\s+-m\s+http\.server|bash\s+-i|nc\s+-e|socat\s+)", line):
                reporter.log("HIGH", "ACTIVE_PROCESS", "Phát hiện tiến trình Reverse Shell / HTTP Server đáng ngờ đang chạy!", {
                    "Process": line.strip()
                })
    except Exception:
        pass

    # Kiểm tra kết nối mạng (ss / netstat)
    try:
        net_out = subprocess.check_output(["ss", "-antup"], universal_newlines=True, stderr=subprocess.DEVNULL)
        for line in net_out.splitlines():
            for ip in CAIRN_IPS:
                if ip in line:
                    reporter.log("CRITICAL", "ACTIVE_NETWORK", f"Phát hiện kết nối mạng đang hoạt động tới IP C2 CAIRN ({ip})!", {
                        "Socket": line.strip()
                    })
    except Exception:
        pass

# ==============================================================================
# HÀM CHÍNH (MAIN)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Công cụ Rà soát Pháp y Apache NiFi & Chiến dịch CAIRN (202.61.139.15)")
    parser.add_argument("-d", "--nifi-dir", default="/opt/nifi", help="Đường dẫn thư mục cài đặt Apache NiFi (Mặc định: /opt/nifi)")
    parser.add_argument("--no-archive", action="store_true", help="Bỏ qua việc quét thư mục conf/archive (Mặc định: Quét cả archive để tìm Processor đã bị xóa)")
    parser.add_argument("--days", type=int, default=45, help="Số ngày kiểm tra tệp .nar/.jar mới sửa đổi (Mặc định: 45 ngày)")
    parser.add_argument("-o", "--output-json", default="/tmp/cairn_nifi_forensic_report.json", help="Đường dẫn xuất báo cáo JSON")
    args = parser.parse_args()

    print("=" * 78)
    print(" BẮT ĐẦU RÀ SOÁT PHÁP Y APACHE NIFI - CHIẾN DỊCH CAIRN (202.61.139.15 / .137)")
    print(f" Thư mục mục tiêu : {args.nifi_dir}")
    print(f" Quét Flow Archive: {'Không' if args.no_archive else 'Có (Bao gồm các luồng đã xóa)'}")
    print("=" * 78)

    reporter = ForensicReporter(json_output_path=args.output_json)

    if not os.path.exists(args.nifi_dir):
        print(f"\033[93m[!] Cảnh báo: Thư mục {args.nifi_dir} không tồn tại. Đang tự động tìm kiếm vị trí cài đặt NiFi trên hệ thống...\033[0m")
        detected_dirs = set()
        for p in glob.glob("/opt/**/flow.json.gz", recursive=True) + glob.glob("/var/**/flow.json.gz", recursive=True) + glob.glob("/home/**/flow.json.gz", recursive=True):
            detected_dirs.add(os.path.dirname(os.path.dirname(p)))
        if detected_dirs:
            for d in detected_dirs:
                print(f"[+] Phát hiện thư mục NiFi tại: {d}")
                scan_flow_files(d, not args.no_archive, reporter)
                scan_nifi_logs(d, reporter)
                scan_system_and_persistence(d, args.days, reporter)
        else:
            print("[-] Không tìm thấy thư mục NiFi tự động. Chỉ thực hiện quét ở tầng Hệ điều hành.")
            scan_system_and_persistence("/opt", args.days, reporter)
    else:
        scan_flow_files(args.nifi_dir, not args.no_archive, reporter)
        scan_nifi_logs(args.nifi_dir, reporter)
        scan_system_and_persistence(args.nifi_dir, args.days, reporter)

    reporter.export_summary()

if __name__ == "__main__":
    main()
