# Báo cáo kiểm chứng — 10/11/2026

## Kết quả

Public repository: https://github.com/danieop/nifi-deploy-kit

Đã thực hiện luồng: export từ hai NiFi nguồn → commit/PR → GitHub Actions export
thực tế → upload artifact → download artifact → triển khai/redeploy Docker local.

- Export CI cuối: https://github.com/danieop/nifi-deploy-kit/actions/runs/42751587870
- Artifact: `nifi-export-42751587870`.
- CI chạy trên runner local có đường vào mạng nội bộ, label `nifi-network`.
- Runner được đăng ký tạm, tự gỡ sau một job; không cài runner trên VM nguồn.
- Source API chỉ dùng GET; dependency được đọc qua SSH. Không dừng/restart/sửa
  cấu hình, không POST/PUT/DELETE trên NiFi nguồn.
- Native sensitive parameter values không nằm trong flow artifact. Không có
  bước quét script/URL/non-sensitive content bổ sung theo lựa chọn của người dùng.
- Người dùng đã xác nhận công khai hai flow cùng bốn binary NAR/JAR được liệt kê.

## Hai deployment đang chạy local

| Nội dung | Bundle node235 | Bundle node237 |
|---|---:|---:|
| Nguồn | 10.0.0.235:9080 | 10.9.3.237:9080 |
| Docker Compose project | ndk-test-node235 | ndk-test-node237 |
| URL | http://localhost:19080/nifi/ | http://localhost:19081/nifi/ |
| NiFi | 2.12.0 | 2.12.0 |
| Process Groups trong ứng dụng (gồm group import) | 5 | 217 |
| Processors | 122 | 1.070 |
| Controller Services | 7 | 3 |
| Connections | 151 | 1.787 |
| Input / output ports | 28 / 0 | 222 / 254 |
| Parameter Contexts được flow sử dụng | 1 | 1 |
| Processor đang RUNNING | 0 | 0 |
| FlowFiles queued | 0 | 0 |
| Type/bundle không khớp artifact | 0 | 0 |
| Registry | Healthy | Không bật |

UI dùng tài khoản Nginx đã cấp riêng, không ghi mật khẩu trong báo cáo.

SHA-256 của flow artifact tải từ CI và đã triển khai:

- node235: `752f5041d7f9a34043e2abfbcd66b8411ac59c8c7f01a601592781103cc0b66a`
- node237: `73b7f52082d9f16a155841798221c927af3806abc08ed848b856e236c13a63e0`

Deployment state/private reports nằm trong working copy dưới `local/runtime-node235`
và `local/runtime-node237`. Thư mục local bị gitignore. NiFi config/repositories/
state nằm trong named volumes riêng của mỗi project.

## Các kiểm tra đã thực hiện

1. 13 unit tests: native export staging/sensitivity, recursive counts, checksum,
   path traversal, literal env parsing, parameter updates, import entity schema,
   dependency ordering, type verification, disabled-state restoration và direct
   component property override.
2. GitHub Actions: Python tests, shell syntax, bundle checksums và Compose config.
3. Export live bằng API root download với referenced services và không component state.
4. Packaged/checksummed custom NARs 1.0.3.1 và jsoup 1.18.3 đọc từ nguồn.
5. Import native snapshot qua đúng ProcessGroupUploadEntity của NiFi 2.12.
6. Đếm lại toàn bộ hierarchy, processor/service type/bundle, ports và parameter contexts.
7. Redeploy cùng bundle giữ nguyên group ID, chỉ một application group, giữ named volumes.
8. Parameter update trên local giữ nguyên 15 parameter không bị mất do cập nhật một key.
9. Direct service property update trên local giữ các property không được nhắc đến.
10. Flow khác checksum bị từ chối khi không có `--overwrite-config`.
11. Thay flow local có chủ đích với flag, queue rỗng và processors stopped thành công;
    phục hồi lại đúng artifact CI mà không đổi group ID hoặc volume.
12. Registry fresh volume được init owner đúng; HTTP API healthcheck đạt.
13. Đọc lại nguồn ở cuối phiên: runningCount 145 / 1.254 và invalidCount 1 / 3
    tương ứng node235 / node237; không chủ động thay đổi nguồn.

Các lần thử ban đầu đã phát hiện và sửa schema import, enum scheduledState và
quyền fresh Registry volume trước khi chốt lần kiểm chứng cuối.

## Giới hạn kiểm chứng

Đây là thành công của **hạ tầng + export/import/redeploy**, không phải xác nhận
đã chạy dữ liệu nghiệp vụ. Cả hai flow được để staged; services chưa được enable
và secrets của RabbitMQ/Kafka/Elasticsearch/API chưa được cấp lại trên local.

Mẫu đo cuối có 39 / 27 processor invalid trong trạng thái staged. Có thể gồm
service chưa enable, parameter/credential thiếu và lỗi cấu hình kế thừa từ nguồn.
Không suy ra tất cả lỗi chỉ do secret. `configurationComplete=false` phản ánh
đúng trạng thái này; admin phải xử lý validation trước khi dùng `--start`.

Parameter contexts không được flow dùng không thuộc snapshot này. GitLab Registry
Clients chỉ được ghi inventory, chưa tự tái tạo token/client. Không chuyển source
queue, checkpoint, provenance hay Registry data; Registry của profile mới khởi
tạo storage mới. Không nâng phiên bản NiFi thông qua redeploy.

## Các bước admin phải thực hiện

### Quản trị export CI

1. Repo Secrets `NIFI_SOURCE_USERNAME`, `NIFI_SOURCE_PASSWORD` và Variables
   `SOURCE_235_URL`, `SOURCE_237_URL` đã được cấu hình cho lần test.
2. Cho các lần sau: duy trì một trusted self-hosted runner label `nifi-network`
   có thể truy cập NiFi nguồn. Hiện runner tạm đã tự gỡ; không có runner thường trú.
3. Chạy Actions → Export NiFi → Run workflow; chọn nguồn và publish branch nếu muốn.
4. Kiểm tra CI, tải artifact hoặc mở PR từ nhánh export và merge main.
5. Chọn/tag release bàn giao; không dùng branch chưa kiểm tra cho production.

### Trên máy khách

1. Có Docker + Compose, Python 3.10+, quyền Docker, RAM/storage và kết nối tới image/artifact.
2. Clone repo, checkout release; tạo folder `local` quyền 700.
3. Copy `examples/deploy.env.example` thành `local/deploy.env`, quyền 600; đặt project,
   thư mục có quyền ghi, port, tài khoản Nginx và heap phù hợp.
4. Tạo `local/parameters.local.json` theo context/key trong bundle. Cấp lại secrets
   và endpoint của môi trường khách. Không đưa các file thực tế lên repo public.
5. Nếu có property chưa parameter hóa, tạo optional `local/components.local.json`
   theo example và dùng `--components` (component phải stopped/disabled).
6. Chạy deploy staged, ví dụ:

   ```bash
   ./deploy.sh --bundle bundles/node235 --env local/deploy.env \
     --parameters local/parameters.local.json
   ```

7. Mở UI, đọc report trong deployment directory và xử lý validation. Đảm bảo
   endpoint/queue/topic/index đúng môi trường khách và backend truy cập được.
8. Khi đủ điều kiện, chạy lại với `--start`. Script enable service được tham chiếu
   theo dependency, giữ processor disabled và từ chối start khi validation chưa đạt.
9. Lần sau dùng lại local files và deployment directory. Cùng flow không import trùng;
   thay flow dùng `--overwrite-config` sau khi dừng và drain máy đích.
10. Giữ các named volumes và file state. Không `down -v`, không prune/reset dữ liệu
    để thử lại. Nếu cần dừng demo local, dùng `docker stop` đúng container/project;
    không xóa volume.

Chi tiết thêm: [Admin guide](admin-vi.md).

## Repository permissions

Không thay đổi quyền mặc định hoặc quyền phê duyệt PR của GitHub Actions. Thiết lập
vẫn là workflow read mặc định, `can_approve_pull_request_reviews=false`. Workflow
export chỉ xin quyền ghi contents để tạo nhánh; PR/merge thực hiện bằng tài khoản
maintainer. Yêu cầu bật quyền duyệt PR trước đó bị auto-review chặn và đã được thay
bằng luồng không cần thay đổi thiết lập này.
