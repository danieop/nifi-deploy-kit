# Kiểm chứng schema 2 — 10/11/2026

## Phạm vi

Export từ hai VM được cung cấp, đọc cả NiFi và Docker Compose/container launch
settings. VM nguồn chỉ có API GET, SSH đọc Docker config/inspect/image metadata và
file bind; không ghi cấu hình, không exec/restart container hay đổi flow nguồn.
SSH/sudo/API có thể phát sinh audit log bình thường.

Bundle mới có đúng hai file JSON cấp cao nhất: `flow.json`, `config.json`; dependency
thật được copy trong `assets/`. Không cần env/parameter/component file riêng.

## GitHub Actions và artifact bàn giao

- [Export NiFi + Docker CI thành công](https://github.com/danieop/nifi-deploy-kit/actions/runs/42763637712).
- Artifact `nifi-export-42763637712` được tải xuống máy local, validate checksum và
  dùng để chuẩn bị config máy đích rồi chạy deploy lại cả hai bundle.
- So sánh toàn bộ config.json, flow checksum, Docker model và assets metadata:
  giống hoàn toàn với bundle đã commit trên main ở cả hai nguồn. Export lại không
  có thay đổi nên CI không tạo nhánh refresh thừa.
- [Validate của PR triển khai](https://github.com/danieop/nifi-deploy-kit/actions/runs/42763566377)
  đạt, gồm 29 unit tests; code được merge qua PR #4.
- Runner ephemeral chỉ nhận một job, tự unregister sau khi thành công. Kiểm tra
  API GitHub trả runner count 0. Các lần export sau cần runner `nifi-network` online.
- Credentials API/SSH nằm trong GitHub Actions Secrets, không có trong config
  public. Payload mở rộng đã được người dùng xác nhận cho phép công khai.

## Kết quả local

| Nội dung | node235 | node237 |
|---|---:|---:|
| NiFi | 2.12.0 | 2.12.0 |
| Compose project | ndk-schema2-node235 | ndk-schema2-node237 |
| UI | http://localhost:19180/nifi/ | http://localhost:19181/nifi/ |
| Process groups trong application | 5 | 217 |
| Processors | 122 | 1.070 |
| Controller services | 7 | 3 |
| Connections | 151 | 1.787 |
| Input/output ports | 28/0 | 222/254 |
| Parameter contexts được dùng | 1 | 1 |
| Type/bundle mismatches | 0 | 0 |
| Property không-sensitive đã đối chiếu | 927 | 4.537 |
| Property mismatches | 0 | 0 |
| Running processors / queued FlowFiles | 0 / 0 | 0 / 0 |
| NiFi Registry HTTP API | Đạt | Không có service |
| File assets được package | 7 | 4 |

Heap local được chỉnh thành 512m/2g; source Docker heap 1g/16g được giữ trong
bundle public để admin chủ động chỉnh theo máy khách. Ports/bind IP local được
chỉnh trong chính config.json. UI dùng tài khoản đã cấp riêng, không ghi password
trong báo cáo. Các demo v0.1 và bản thử trung gian đã dừng, volumes được giữ lại.

## Kiểm tra đã thực hiện

- 29 unit tests: native import schema, sensitivity/secret presence, referenced
  parameters, checksum/path traversal, literal env/dollar handling, source-ID
  mapping, duplicated versioned IDs, mount ordering, atomic export cleanup,
  Docker change/queue guards và legacy compatibility.
- UI Basic Auth: cả hai UI trả 401 khi chưa đăng nhập, 200 khi dùng credentials
  local. Hai Compose JSON từ bundle đều qua `docker compose config --quiet`.
- Đọc nguồn lần cuối bằng GET: runningCount 145 / 1.254, invalidCount 1 / 3,
  tương ứng node235 / node237.
- Fresh import cả hai flow bằng Compose thực tế được export, image pin theo digest.
- Đối chiếu hierarchy, types/bundles và các property không-sensitive với config.
  API che cả parameter references sensitive nên không suy diễn plaintext từ GET.
- Sửa `Connect Timeout` của một controller service trong config.json từ 5000 lên
  6000, deploy lại, đọc API xác nhận; phục hồi 5000 thành công.
- Cùng config tái sử dụng group ID và volumes, không tạo application group trùng.
- Ghi đè có chủ đích bằng `--overwrite-config` khi target stopped/queue rỗng;
  backup native flow được lưu riêng trong runtime.
- Structured masking trên cả flow.json và config.json; kiểm tra các credential
  đăng nhập/SSH được cung cấp không nằm trong payload sẽ commit.

Các ca thực tế phát hiện hai vấn đề đã được sửa và có regression tests:
versioned identifier có thể trùng giữa group được copy (302 ID trùng trên .237),
và Docker Mounts không có thứ tự ổn định. Override dùng source instanceIdentifier;
volume/mount identity dùng đường dẫn đích thay vì index.

## Giới hạn

Các processor được để staged. Sau validation bất đồng bộ, mẫu đo có 39 / 27
processor invalid tương ứng node235 / node237; còn 5 / 11 placeholder NiFi chưa
cấp. `configurationComplete=false`. Không chạy dữ liệu nghiệp vụ và không xác
nhận kết nối các backend của khách. Secret/endpoint, service enable và lỗi kế
thừa phải được admin xử lý trước --start.

Source .237 thực tế chỉ có API NAR trong mount extensions; bản export mới phản
ánh đúng mount đó, không tự thêm hai implementation NAR từ máy .235 như bundle
v0.1. Những custom processors không có implementation ở nguồn vốn disabled.

Registry .235 nguồn không có storage mount; model export giữ tính ephemeral đó.
Không chuyển Registry data hay source repositories/state/queue. Không cam kết
mask credential viết trong script/URL/non-sensitive text hoặc binary.

Hướng dẫn thao tác: [admin-vi.md](admin-vi.md). Chi tiết các trường:
[config-reference.md](config-reference.md).
