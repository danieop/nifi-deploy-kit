# Kiểm chứng workflow export server tùy ý — 10/11/2026

## Thay đổi đã kiểm chứng

Workflow nhận server và tên bundle qua dispatch inputs, credential profile riêng
qua tên GitHub Secret. Không còn đọc source inventory hoặc Secrets theo số IP.
Một lần chạy chỉ export/validate/upload một bundle; publish tùy chọn tạo nhánh
chỉ chứa dữ liệu bundle đó. Format flow/config vẫn schema 2.

## Kiểm tra tự động

- 36 unit tests đạt: giữ các kiểm tra native flow/deploy cũ, bổ sung source name
  tùy ý, custom project/SSH port/service/group, URL/path/SSH input preflight,
  JSON profile, credential environment/file cleanup, log mask và project name dài.
- Official actionlint kiểm tra workflow syntax đạt; binary được kiểm checksum
  từ bản phát hành chính thức trước khi chạy.
- GitHub Validate của [PR #6](https://github.com/danieop/nifi-deploy-kit/pull/6)
  đạt trước khi merge main.

## Hai lần export GitHub Actions thực tế

| Nội dung | Ca A | Ca B |
|---|---|---|
| Run | [42771495027](https://github.com/danieop/nifi-deploy-kit/actions/runs/42771495027) | [42771638940](https://github.com/danieop/nifi-deploy-kit/actions/runs/42771638940) |
| Kết quả | Success | Success |
| Nguồn được nhập | 10.0.0.235 | 10.9.3.237 |
| bundle_name | generic-source-a | generic-source-b |
| Credential Secret | DEMO_NODE235_EXPORT | DEMO_NODE237_EXPORT |
| Runner label nhập | generic-export-test | generic-export-test |
| publish | false | true |
| Processors | 122 | 1.070 |
| Controller services | 7 | 3 |
| Process groups | 5 | 217 |
| Packaged assets | 7 | 4 |

Từng run dùng credential profile khác nhau, API URL/SSH host/Compose path được
nhập trực tiếp. Hai VM chỉ có GET API và SSH Docker/file reads như các lần trước;
không restart/exec container hoặc sửa flow/cấu hình trên nguồn.

Đã download artifact của cả hai run và validate checksum/schema. Root artifact
chỉ gồm `flow.json`, `config.json`, `assets/`; không có các bundle nguồn khác.
Native flow checksum khớp bản nguồn đã bàn giao; bundle identity/project/runtime
path mang tên tùy ý được nhập. Gateway password vẫn là placeholder.

Ca B tạo nhánh
`Feat/refresh-nifi-export-generic-source-b-42771638940`. Diff với main chỉ nằm dưới
`bundles/generic-source-b/`, không đụng node235/node237 hay file repo khác. Nhánh
này được giữ để minh họa/review; không tự merge dữ liệu demo vào main.

Mỗi runner ephemeral nhận một job và tự unregister; API GitHub sau test trả
runner count **0**. Các lần chạy sau cần trusted runner có label phù hợp online.
Hai profile demo đã được cấu hình trong repository Secrets; credential profile
truy cập nguồn không được ghi vào docs, workflow inputs, bundle hoặc artifact.

## Phạm vi

Đây là kiểm chứng orchestration export generic. Không chạy luồng nghiệp vụ hay
deploy thêm instance Docker cho thay đổi workflow này. Kiểm chứng import/redeploy
schema 2 nằm trong [báo cáo schema 2](test-report-schema2-2026-11-10.md); các instance
local đó được giữ staged. Secret và validation vẫn phải được admin cấp/xử lý.

Một host tùy ý vẫn phải thuộc baseline Docker Compose + NiFi/Nginx được hỗ trợ,
và runner phải truy cập được nó. Không suy diễn hỗ trợ mọi phiên bản NiFi, cluster,
Kubernetes hoặc phương thức authentication từ việc workflow nhận host tùy ý.

Hướng dẫn thao tác: [export-workflow.md](export-workflow.md).
