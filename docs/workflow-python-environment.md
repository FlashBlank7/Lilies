# 工作流 Python 运行环境

工作流的 Bash 节点在 Docker 内运行。宿主机或后台虚拟环境装包不会改变工作流容器。默认镜像预装 NumPy、SciPy、pandas、scikit-learn、joblib 等标准工具；版本固定在仓库根目录 requirements-sandbox.txt，构建时安装，业务运行无需联网。

## 页面配置代码和变量

画布新增工具节点，工具名填写 `Bash`。`input.command` 放固定程序，`input.stdin` 放文本、JSON 对象或上游引用。对象会序列化为 JSON 送入标准输入，Python 可用 `json.load(sys.stdin)` 读取；不要把输入资料直接拼进 Shell 命令。

```json
{
  "tool_name": "Bash",
  "input": {
    "command": "python3 -c 'import json,sys; data=json.load(sys.stdin); print(json.dumps(data, ensure_ascii=False))'",
    "stdin": {"$ref":{"node_id":"$inputs","path":[]}},
    "timeout": 300
  }
}
```

返回字段包括 `stdout`、`stderr`、`exit_code`；标准输出为完整 JSON 时另有 `json`，可通过 `{"$ref":{"node_id":"节点ID","path":["json","字段名"]}}` 引用。旧 `output` 仍包含文本和退出码，保持兼容。命令失败会显示实际错误，不作为成功结果继续。

标准输出和错误输出各最多返回 200,000 字符。Bash 超过上限会明确报告截断并停止该节点，不会将截断内容当作完整 JSON 继续传递。大量源码、索引或报告应写入工作目录文件，按需求分批读取，并在业务结果中保留实际读取范围。

结束节点的 `outputs` 是字段映射，按字段引用上游值，例如 `{"markdown":{"$ref":{"node_id":"报告节点","path":["json","markdown"]}},"artifacts":{"$ref":{"node_id":"报告节点","path":["json","artifacts"]}}}`。项目页面显示 `markdown`，并将 `artifacts` 中的 `file_path` 与 `label` 显示为下载入口。每次业务报告应放在独立运行目录；保存节点后等待保存提示，再进入试运行。

文档环境使用 `Dockerfile.documents`，详见[项目模型连接](project-model-connections.md#文档运行环境)。9/17 页面实际运行已验证 pypdf、python-docx、reportlab、Pillow、Poppler、Tesseract 可用并生成中文 PDF；这仅证明运行环境，评审结论仍需对照原始资料检查。

## 更新运行环境

在 Bash 节点执行 `python /opt/platform/check_sandbox_ml.py`，可以核对实际包版本，并用构造数据验证标准预处理、时间拆分、拟合、预测、模型保存和加载。返回的 `scope=synthetic_environment_check` 表示环境验证，不能作为项目算法的业务精度结果。普通运行和保存测试均应执行该检查。

需要新依赖时，搭建者报告包名、用途及影响，并依用户已有授权补充运行环境。标准工具缺失不意味着应手写替代库或削减企业要求。

更新流程：

1. 选择与现有 Python 和数值库兼容的版本，更新 requirements-sandbox.txt，避免无关依赖升级。
2. `docker build -f Dockerfile.sandbox -t agent-platform-sandbox:<版本> .`
3. `docker run --rm --network none agent-platform-sandbox:<版本> python /opt/platform/check_sandbox_ml.py`
4. 设置现有 SANDBOX_IMAGE 使用版本标签；或保留旧镜像标签后，将已验证镜像标为 agent-platform-sandbox:latest，供新容器使用。已有容器继续使用原镜像，不能拿它们验证新环境。
5. 验证项目普通运行和保存测试，并运行受影响的后端测试。

dev_platform.sh 只在镜像不存在时构建。已有镜像不会因修改清单或重启应用自动更新，必须执行上述显式构建与切换。
