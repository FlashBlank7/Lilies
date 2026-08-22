import json

file_path = 'old_sc.json'

try:
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
        # 验证数据是否符合给定格式结构
        if isinstance(data, dict) and 'vcalendar' in data:
            print("文件以JSON格式读取成功，且数据结构符合预期")
        else:
            print("文件以JSON格式读取成功，但数据结构不符合预期")
except FileNotFoundError:
    print(f"错误：未找到文件 {file_path}")
except json.JSONDecodeError:
    print(f"错误：文件 {file_path} 不是有效的JSON格式")
except Exception as e:
    print(f"读取文件 {file_path} 时发生未知错误: {e}")

if 'data' in locals():
    print(data)
