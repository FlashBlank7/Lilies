import pandas as pd
import os
from scipy.stats import pearsonr
import matplotlib.pyplot as plt
# 设置 matplotlib 的字体，解决中文显示问题
plt.rcParams['font.sans-serif'] = ['SimHei']
# 解决负号显示问题
plt.rcParams['axes.unicode_minus'] = False


# 所有满减活动的促销代码集合
full_discount_codes = {
    "PH2211CV1", "PH2211CV2", "PH2211CV3", "PH2211CV4",
    "PH2301CV1", "PH2301CV2", "PH2301CV3", "PH2301CV4", "PH2301CV5",
    "PM2211CV12", "PM2211CV2", "PM2211CV3", "PM2211CV4", "PM2211CV5", 
    "PM2211CV6", "PM2211CV7", "PM2211CV8",
    "PM2301CV1", "PM2301CV2", "PM2301CV3", "PM2301CV4", "PM2301CV5",
    "PH2301SS2", "PM2301CV6", "PM2301J03"
}

def read_excel_file(file_path):
    """
    读取 Excel 文件并返回包含所有子表数据的字典
    :param file_path: Excel 文件路径
    :return: 包含所有子表数据的字典，如果出错则返回空字典
    """
    try:
        excel_file = pd.ExcelFile(file_path)
        data_frames = {}
        for sheet_name in excel_file.sheet_names:
            data_frames[sheet_name] = excel_file.parse(sheet_name)
        return data_frames
    except FileNotFoundError:
        print(f"文件 {file_path} 未找到，请检查文件路径。")
        return {}
    except Exception as e:
        print(f"读取文件时发生错误: {e}")
        return {}

def calculate_order_stats(df):
    """
    计算 VIP 用户和普通用户的交易单量及差异，同时计算 VIP 和普通用户的销售总额及差异，以及利润率，还有退货率和促销相关率，
    并计算 VIP 与普通用户平均每单的消费
    :param df: 销售交易明细表数据框
    :return: VIP 用户交易单量, 普通用户交易单量, 交易单量差异, 交易单量比例, VIP 用户销售总额, 普通用户销售总额, 销售总额差异, 销售总额比例, 总利润率, VIP 用户利润率, 普通用户利润率, VIP 与普通用户毛利润比例, VIP 用户退货率, 普通用户退货率, VIP 用户促销相关单量, 普通用户促销相关单量, VIP 用户促销相关率, 普通用户促销相关率, VIP 用户平均每单消费, 普通用户平均每单消费
    """
    # 计算 RTLPC_C 加上折扣金额后的值
    df['调整后价格'] = df['RTLPC_C'] + df['折扣金额']
    # 筛选出调整后价格大于等于 0 的有效记录
    valid_df = df[df['调整后价格'] >= 0]
    
    # 筛选 VIP 和普通客户的有效数据
    vip_valid_df = valid_df[valid_df['VIP客户姓名'].notna()]
    normal_valid_df = valid_df[valid_df['VIP客户姓名'].isna()]
    
    # 按复合主键前缀销售单据编号分组统计单量
    vip_orders = vip_valid_df.groupby('复合主键前缀销售单据编号').ngroups
    normal_orders = normal_valid_df.groupby('复合主键前缀销售单据编号').ngroups
    
    order_diff = vip_orders - normal_orders
    total_orders = vip_orders + normal_orders
    order_ratio = vip_orders / normal_orders if normal_orders != 0 else float('inf')

    # 筛选普通用户数据
    normal_df = df[df['VIP客户姓名'].isna()]
    normal_sales_series = normal_df['调整后价格']
    
    # 定义 3sigma 过滤函数
    def filter_by_3sigma(data):
        mean = data.mean()
        std = data.std()
        lower_bound = mean - 3 * std
        upper_bound = mean + 3 * std
        return data[(data >= lower_bound) & (data <= upper_bound)]
    
    # 过滤普通用户销售额异常值
    normal_sales_series_filtered = filter_by_3sigma(normal_sales_series)
    normal_sales = normal_sales_series_filtered.sum()

    vip_sales = df[df['VIP客户姓名'].notna()]['调整后价格'].sum()
    sales_diff = vip_sales - normal_sales
    sales_ratio = vip_sales / normal_sales if normal_sales != 0 else float('inf')

    # 计算总毛利润和各用户类型的毛利润
    total_gross_profit = df['后毛利润'].sum()
    vip_gross_profit = df[df['VIP客户姓名'].notna()]['后毛利润'].sum()
    normal_gross_profit = normal_df[normal_df['调整后价格'].isin(normal_sales_series_filtered)]['后毛利润'].sum()

    # 计算利润率
    total_profit_rate = total_gross_profit / df['调整后价格'].sum() if df['调整后价格'].sum() != 0 else 0
    vip_profit_rate = vip_gross_profit / vip_sales if vip_sales != 0 else 0
    normal_profit_rate = normal_gross_profit / normal_sales if normal_sales != 0 else 0

    # 计算 VIP 与普通用户毛利润比例
    gross_profit_ratio = vip_gross_profit / normal_gross_profit if normal_gross_profit != 0 else float('inf')

    # 筛选 VIP 和普通客户数据
    vip_df = df[df['VIP客户姓名'].notna()]
    normal_df = df[df['VIP客户姓名'].isna()]

    # 计算 VIP 用户退货记录数
    vip_return_records = vip_df[(vip_df['调整后价格'] < 0) & (vip_df['物料类型描述'] != 'Gift')].shape[0]
    # 计算 VIP 用户退货率
    # 修正分母逻辑，使用 vip_orders 作为分母
    vip_return_rate = vip_return_records / vip_orders if vip_orders != 0 else 0

    # 计算普通用户退货记录数
    normal_return_records = normal_df[(normal_df['调整后价格'] < 0) & (normal_df['物料类型描述'] != 'Gift')].shape[0]
    # 计算普通用户退货率
    # 修正分母逻辑，使用 normal_orders 作为分母
    normal_return_rate = normal_return_records / normal_orders if normal_orders != 0 else 0

    # 计算 VIP 用户促销相关单量
    vip_promo_orders = vip_valid_df[vip_valid_df['促销代码'].notna()].groupby('复合主键前缀销售单据编号').ngroups
    # 计算普通用户促销相关单量
    normal_promo_orders = normal_valid_df[normal_valid_df['促销代码'].notna()].groupby('复合主键前缀销售单据编号').ngroups
    # 计算 VIP 用户促销相关率
    vip_promo_rate = vip_promo_orders / vip_orders if vip_orders != 0 else 0
    # 计算普通用户促销相关率
    normal_promo_rate = normal_promo_orders / normal_orders if normal_orders != 0 else 0

    # 按促销编号分组计算每单消费
    vip_consumption = vip_df.groupby('促销编号')['调整后价格'].sum()
    normal_consumption = normal_df.groupby('促销编号')['调整后价格'].sum()

    # 过滤掉负值
    vip_consumption = vip_consumption[vip_consumption >= 0]
    normal_consumption = normal_consumption[normal_consumption >= 0]

    # 计算平均每单消费
    vip_avg_consumption = vip_consumption.mean() if len(vip_consumption) > 0 else 0
    normal_avg_consumption = normal_consumption.mean() if len(normal_consumption) > 0 else 0

    return vip_orders, normal_orders, order_diff, order_ratio, vip_sales, normal_sales, sales_diff, sales_ratio, total_profit_rate, vip_profit_rate, normal_profit_rate, gross_profit_ratio, vip_return_rate, normal_return_rate, vip_promo_orders, normal_promo_orders, vip_promo_rate, normal_promo_rate, vip_avg_consumption, normal_avg_consumption

def save_results_to_txt(*args, template=None, output_path='analysis_stats.txt', overwrite=True):
    """
    将统计结果保存到 txt 文件
    :param args: 不限数量的参数
    :param template: 模板字符串，用于格式化参数
    :param output_path: 输出文件路径，默认为 'order_stats.txt'
    :param overwrite: 是否覆盖写入，默认为 True
    """
    try:
        mode = 'w' if overwrite else 'a'
        with open(output_path, mode, encoding='utf-8') as f:
            if template:
                f.write(template.format(*args))
            else:
                for arg in args:
                    f.write(str(arg) + '\n')
        print(f"结果已保存到 {output_path}")
    except Exception as e:
        print(f"保存结果到文件时发生错误: {e}")

def add_percentage_labels(ax, data):
    """
    在柱状图上添加百分比标签
    :param ax: matplotlib 的 Axes 对象
    :param data: 数据序列
    """
    total = data.sum()
    for p in ax.patches:
        height = p.get_height()
        percent = (height / total) * 100
        ax.annotate(f'{percent:.1f}%', (p.get_x() + p.get_width() / 2., height),
                    ha='center', va='bottom', fontsize=8)

def plot_material_distribution(df, output_folder='analysis_plots'):
    """
    统计物料类型描述的取值，以及 VIP 和普通客户购买时物料类型分布情况，并绘制成图保存
    :param df: 销售交易明细表数据框
    :param output_folder: 保存图片的文件夹路径，默认为 'analysis_plots'
    """
    # 确保保存图片的文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # 统计物料类型描述的取值
    material_values = df['物料类型描述'].value_counts()
    print("物料类型描述的取值统计：")
    print(material_values)

    # 筛选 VIP 和普通客户数据
    vip_df = df[df['VIP客户姓名'].notna()]
    normal_df = df[df['VIP客户姓名'].isna()]

    # 统计 VIP 和普通客户的物料类型分布
    vip_material_dist = vip_df['物料类型描述'].value_counts()
    normal_material_dist = normal_df['物料类型描述'].value_counts()

    # 设置图片清晰度
    plt.rcParams['figure.dpi'] = 300

    # 绘制 VIP 客户物料类型分布柱状图
    plt.figure(figsize=(12, 6))
    ax1 = vip_material_dist.plot(kind='bar')
    plt.title('VIP客户物料类型分布')
    plt.xlabel('物料类型描述')
    plt.ylabel('数量')
    plt.xticks(rotation=45)
    # 添加百分比标签
    add_percentage_labels(ax1, vip_material_dist)
    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'vip_material_distribution.png'))
    plt.close()

    # 绘制普通客户物料类型分布柱状图
    plt.figure(figsize=(12, 6))
    ax2 = normal_material_dist.plot(kind='bar')
    plt.title('普通客户物料类型分布')
    plt.xlabel('物料类型描述')
    plt.ylabel('数量')
    plt.xticks(rotation=45)
    # 添加百分比标签
    add_percentage_labels(ax2, normal_material_dist)
    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'normal_material_distribution.png'))
    plt.close()

    print(f"物料类型分布图片已保存到 {output_folder} 文件夹")

def plot_order_consumption_distribution(df, output_folder='analysis_plots'):
    """
    按促销编号分组计算每单消费，将 VIP 用户与普通用户的单分开，绘制消费分布图并添加百分比
    :param df: 销售交易明细表数据框
    :param output_folder: 保存图片的文件夹路径，默认为 'analysis_plots'
    """
    # 定义该函数专用的 bins 数量
    ORDER_CONSUMPTION_BINS = 20
    
    # 确保保存图片的文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # 设置图片清晰度
    plt.rcParams['figure.dpi'] = 300

    # 计算 RTLPC_C 加上折扣金额后的值
    df['调整后价格'] = df['RTLPC_C'] + df['折扣金额']

    # 筛选 VIP 和普通客户数据
    vip_df = df[df['VIP客户姓名'].notna()]
    normal_df = df[df['VIP客户姓名'].isna()]

    # 按促销编号分组计算每单消费
    vip_consumption = vip_df.groupby('促销编号')['调整后价格'].sum()
    normal_consumption = normal_df.groupby('促销编号')['调整后价格'].sum()

    # 过滤掉负值
    vip_consumption = vip_consumption[vip_consumption >= 0]
    normal_consumption = normal_consumption[normal_consumption >= 0]

    # 定义 3sigma 过滤函数
    def filter_by_3sigma(data):
        mean = data.mean()
        std = data.std()
        lower_bound = mean - 3 * std
        upper_bound = mean + 3 * std
        return data[(data >= lower_bound) & (data <= upper_bound)]

    # 过滤异常数据
    vip_consumption_filtered = filter_by_3sigma(vip_consumption)
    normal_consumption_filtered = filter_by_3sigma(normal_consumption)

    # 辅助函数：在直方图上添加消费值标签
    def add_consumption_labels(ax, data):
        # 先绘制直方图并获取返回值
        n, bins, patches = ax.hist(data, bins=ORDER_CONSUMPTION_BINS, edgecolor='black', density=True)
        for i, rect in enumerate(ax.patches[:len(n)]):  # 确保只遍历与 bin_counts 长度一致的柱子
            height = rect.get_height()
            x = rect.get_x()
            width = rect.get_width()
            center_x = x + width / 2
            print(f"bin: {i}, height: {height}, x: {x}, width: {width}, center_x: {center_x}")
            # 只显示高度不为 0 的柱子的标签
            if height > 0:
                ax.text(center_x, height, f'{center_x:.2f}', ha='center', va='bottom', fontsize=8)

    # 打印 VIP 和普通用户的消费情况
    print("VIP 用户每单消费情况：")
    print(vip_consumption_filtered)
    print("普通用户每单消费情况：")
    print(normal_consumption_filtered)

    # 绘制 VIP 用户消费分布图
    plt.figure(figsize=(12, 6))
    ax1 = plt.gca()
    n, bins, patches = ax1.hist(vip_consumption_filtered, bins=ORDER_CONSUMPTION_BINS, edgecolor='black', density=True)
    plt.title('VIP用户消费分布')
    plt.xlabel('每单消费金额')
    plt.ylabel('频率')

    # 注释掉添加消费值标签的代码
    # add_consumption_labels(ax1, vip_consumption_filtered)

    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'vip_consumption_distribution.png'))
    plt.close()

    # 绘制普通用户消费分布图
    plt.figure(figsize=(12, 6))
    ax2 = plt.gca()
    n, bins, patches = ax2.hist(normal_consumption_filtered, bins=ORDER_CONSUMPTION_BINS, edgecolor='black', density=True)
    plt.title('普通用户消费分布')
    plt.xlabel('每单消费金额')
    plt.ylabel('频率')

    # 注释掉添加消费值标签的代码
    # add_consumption_labels(ax2, normal_consumption_filtered)

    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'normal_consumption_distribution.png'))
    plt.close()

    print(f"消费分布图片已保存到 {output_folder} 文件夹")

def plot_daily_sales(df, output_folder='analysis_plots', show_labels=True):
    """
    按发货日期分组计算每日零售总额和每日毛利润，绘制时间序列图并保存
    :param df: 销售交易明细表数据框
    :param output_folder: 保存图片的文件夹路径，默认为 'analysis_plots'
    :param show_labels: 是否显示销售额和毛利润标注，默认为 True
    :return: 包含每日零售总额、VIP 每日零售总额、普通用户每日零售总额、每日毛利润、VIP 每日毛利润和普通用户每日毛利润的 DataFrame
    """
    # 定义该函数专用的 bins 数量（虽然此函数未使用直方图，但遵循规范定义）
    DAILY_SALES_BINS = 10
    
    # 确保保存图片的文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    
    # 设置图片清晰度
    plt.rcParams['figure.dpi'] = 300
    
    # 将发货日期转换为日期时间类型，并只保留日期部分
    df['发货日期'] = pd.to_datetime(df['发货日期']).dt.date
    
    # 计算 RTLPC_C 加上折扣金额后的值
    df['调整后价格'] = df['RTLPC_C'] + df['折扣金额']

    # 筛选 VIP 和普通客户数据
    vip_df = df[df['VIP客户姓名'].notna()]
    normal_df = df[df['VIP客户姓名'].isna()]

    # 按发货日期分组计算每日零售总额
    daily_sales = df.groupby('发货日期')['调整后价格'].sum()
    vip_daily_sales = vip_df.groupby('发货日期')['调整后价格'].sum()
    
    # 定义 3sigma 过滤函数
    def filter_by_3sigma(data):
        mean = data.mean()
        std = data.std()
        lower_bound = mean - 3 * std
        upper_bound = mean + 3 * std
        return data[(data >= lower_bound) & (data <= upper_bound)]
    
    normal_daily_sales_series = normal_df.groupby('发货日期')['调整后价格'].sum()
    normal_daily_sales = filter_by_3sigma(normal_daily_sales_series)

    # 按发货日期分组计算每日毛利润
    daily_gross_profit = df.groupby('发货日期')['后毛利润'].sum()
    vip_daily_gross_profit = vip_df.groupby('发货日期')['后毛利润'].sum()
    normal_daily_gross_profit = normal_df[normal_df['调整后价格'].isin(normal_daily_sales_series[normal_daily_sales_series.index.isin(normal_daily_sales.index)])].groupby('发货日期')['后毛利润'].sum()

    # 筛选出调整后价格大于等于 0 的记录
    valid_df = df[df['调整后价格'] >= 0]
    # 筛选出 “促销代码” 不为空的记录
    promo_df = valid_df[valid_df['促销代码'].notna()]

    # 按发货日期分组统计每日交易商品量
    daily_product_count = valid_df.groupby('发货日期').size()
    # 按发货日期分组统计每日与促销有关的商品量
    daily_promo_product_count = promo_df.groupby('发货日期').size()

    # 按发货日期分组计算每日促销商品的零售总额
    daily_promo_sales = promo_df.groupby('发货日期')['调整后价格'].sum()
    # 按发货日期分组计算每日促销商品的后毛利润
    daily_promo_gross_profit = promo_df.groupby('发货日期')['后毛利润'].sum()

    # 筛选普通用户且促销代码不为空的记录
    normal_promo_df = normal_df[normal_df['促销代码'].notna()]
    # 按发货日期分组计算普通用户受促销影响的每日零售总额
    normal_daily_promo_sales = normal_promo_df.groupby('发货日期')['调整后价格'].sum()
    # 按发货日期分组计算普通用户受促销影响的每日总后毛利润
    normal_daily_promo_gross_profit = normal_promo_df.groupby('发货日期')['后毛利润'].sum()

    # 筛选出物料类型描述为 Jewellery - FG & RM，Jewellery - Gold，Watch 的记录
    target_materials = ['Jewellery - FG & RM', 'Jewellery - Gold', 'Watch']
    target_materials_df = df[df['物料类型描述'].isin(target_materials)]

    # 按发货日期和物料类型描述分组，计算每日总销售量和总零售额
    daily_material_sales = target_materials_df.groupby(['发货日期', '物料类型描述']).agg({
        '销售数量': 'sum',
        '调整后价格': 'sum'
    }).unstack(fill_value=0)

    # 保存物料类型销售数据到 Excel 文件
    daily_material_sales.to_excel(os.path.join(output_folder, 'daily_material_sales.xlsx'), index=True)
    print(f"Jewellery - FG & RM，Jewellery - Gold，Watch 的每日总销售量和总零售额数据已保存到 {os.path.join(output_folder, 'daily_material_sales.xlsx')}")

    # 筛选出属于满减活动的促销记录
    full_discount_promo_df = promo_df[promo_df['促销代码'].isin(full_discount_codes)]
    # 筛选出不属于满减活动的促销记录
    non_full_discount_promo_df = promo_df[~promo_df['促销代码'].isin(full_discount_codes)]
    # 筛选出直接跟珠宝首饰相关的满减活动记录
    target_materials = ['Jewellery - FG & RM', 'Jewellery - Gold', 'Watch']

    target_materials_jew = ['Jewellery - FG & RM', 'Jewellery - Gold']
    jewellery_full_discount_promo_df = full_discount_promo_df[full_discount_promo_df['物料类型描述'].isin(target_materials_jew)]

    # 按发货日期分组统计每日与满减活动有关的商品量
    daily_full_discount_product_count = full_discount_promo_df.groupby('发货日期').size()
    # 按发货日期分组统计每日与非满减活动有关的商品量
    daily_non_full_discount_product_count = non_full_discount_promo_df.groupby('发货日期').size()
    # 按发货日期分组统计每日与珠宝首饰相关的满减活动商品量
    daily_jewellery_full_discount_product_count = jewellery_full_discount_promo_df.groupby('发货日期').size()
    # 按发货日期分组统计每日与珠宝首饰相关的非满减活动商品量
    daily_jewellery_non_full_discount_product_count = non_full_discount_promo_df[non_full_discount_promo_df['物料类型描述'].isin(target_materials_jew)].groupby('发货日期').size()

    # 按发货日期分组计算每日满减活动商品的零售总额
    daily_full_discount_sales = full_discount_promo_df.groupby('发货日期')['调整后价格'].sum()
    # 按发货日期分组计算每日非满减活动商品的零售总额
    daily_non_full_discount_sales = non_full_discount_promo_df.groupby('发货日期')['调整后价格'].sum()
    # 按发货日期分组计算每日珠宝首饰相关的满减活动商品的零售总额
    daily_jewellery_full_discount_sales = jewellery_full_discount_promo_df.groupby('发货日期')['调整后价格'].sum()

    # 按发货日期分组计算每日满减活动商品的后毛利润
    daily_full_discount_gross_profit = full_discount_promo_df.groupby('发货日期')['后毛利润'].sum()
    # 按发货日期分组计算每日非满减活动商品的后毛利润
    daily_non_full_discount_gross_profit = non_full_discount_promo_df.groupby('发货日期')['后毛利润'].sum()
    # 按发货日期分组计算每日珠宝首饰相关的满减活动商品的后毛利润
    daily_jewellery_full_discount_gross_profit = jewellery_full_discount_promo_df.groupby('发货日期')['后毛利润'].sum()

    # 合并数据到一个 DataFrame
    sales_data = pd.DataFrame({
        '日零售额': daily_sales,
        '日零售额_VIP': vip_daily_sales,
        '日零售额_普通': normal_daily_sales,
        '日毛利润': daily_gross_profit,
        '日毛利润_VIP': vip_daily_gross_profit,
        '日毛利润_普通': normal_daily_gross_profit,
        '日交易商品量': daily_product_count,
        '日促销商品量': daily_promo_product_count,
        '日促销商品零售额': daily_promo_sales,
        '日促销商品后毛利润': daily_promo_gross_profit,
        '普通用户日促销零售额': normal_daily_promo_sales,
        '普通用户日促销后毛利润': normal_daily_promo_gross_profit,
        '日满减活动商品量': daily_full_discount_product_count,
        '日非满减活动商品量': daily_non_full_discount_product_count,
        '日珠宝首饰相关满减活动商品量': daily_jewellery_full_discount_product_count,
        '日珠宝首饰相关非满减活动商品量': daily_jewellery_non_full_discount_product_count,
        '日满减活动商品零售额': daily_full_discount_sales,
        '日非满减活动商品零售额': daily_non_full_discount_sales,
        '日珠宝首饰相关满减活动商品零售额': daily_jewellery_full_discount_sales,
        '日满减活动商品后毛利润': daily_full_discount_gross_profit,
        '日非满减活动商品后毛利润': daily_non_full_discount_gross_profit,
        '日珠宝首饰相关满减活动商品后毛利润': daily_jewellery_full_discount_gross_profit
        
    }).fillna(0)
    
    # 保存数据到 Excel 文件
    sales_data.to_excel(os.path.join(output_folder, 'daily_sales_data.xlsx'), index=True)
    
    # 计算 VIP 用户的日零售额与总日零售额的关系（皮尔逊相关系数）
    corr, p_value = pearsonr(sales_data['日零售额'], sales_data['日零售额_VIP'])
    print(f"VIP 用户的日零售额与总日零售额的皮尔逊相关系数: {corr:.4f}，p 值: {p_value:.4f}")

    # 绘制每日零售总额时间序列图
    plt.figure(figsize=(12, 6))
    daily_sales.plot(kind='line', marker='o', label='总销售额')
    vip_daily_sales.plot(kind='line', marker='s', label='VIP客户销售额')
    normal_daily_sales.plot(kind='line', marker='^', label='普通客户销售额')
    daily_promo_sales.plot(kind='line', marker='*', label='促销商品销售额')
    normal_daily_promo_sales.plot(kind='line', marker='v', label='普通用户促销销售额')
    daily_non_full_discount_sales.plot(kind='line', marker='<', label='非满减活动商品销售额')
    daily_jewellery_full_discount_sales.plot(kind='line', marker='>', label='珠宝首饰相关满减活动商品销售额')
    
    plt.title('每日零售总额时间序列')
    plt.xlabel('日期')
    plt.ylabel('零售总额')
    plt.xticks(rotation=45)
    
    # 注释掉添加销售额标注的代码
    # if show_labels:
    #     for x, y in zip(daily_sales.index, daily_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,5), ha='center', fontsize=6)
    #     for x, y in zip(vip_daily_sales.index, vip_daily_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,15), ha='center', fontsize=6)  # 拉开 VIP 标注距离
    #     for x, y in zip(normal_daily_sales.index, normal_daily_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,5), ha='center', fontsize=6)
    #     for x, y in zip(daily_promo_sales.index, daily_promo_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,25), ha='center', fontsize=6)  # 拉开促销商品标注距离
    #     for x, y in zip(normal_daily_promo_sales.index, normal_daily_promo_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,35), ha='center', fontsize=6)  # 拉开普通用户促销销售额标注距离
    #     for x, y in zip(daily_non_full_discount_sales.index, daily_non_full_discount_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,45), ha='center', fontsize=6)  # 拉开非满减活动商品销售额标注距离
    #     for x, y in zip(daily_jewellery_full_discount_sales.index, daily_jewellery_full_discount_sales):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,55), ha='center', fontsize=6)  # 拉开珠宝首饰相关满减活动商品销售额标注距离
    
    plt.legend()
    plt.tight_layout()
    
    # 保存图片
    plt.savefig(os.path.join(output_folder, 'daily_sales_distribution.png'))
    plt.close()

    # 绘制每日毛利润时间序列图
    plt.figure(figsize=(12, 6))
    daily_gross_profit.plot(kind='line', marker='o', label='总毛利润')
    vip_daily_gross_profit.plot(kind='line', marker='s', label='VIP客户毛利润')
    normal_daily_gross_profit.plot(kind='line', marker='^', label='普通客户毛利润')
    daily_promo_gross_profit.plot(kind='line', marker='*', label='促销商品毛利润')
    normal_daily_promo_gross_profit.plot(kind='line', marker='v', label='普通用户促销毛利润')
    daily_non_full_discount_gross_profit.plot(kind='line', marker='<', label='非满减活动商品毛利润')
    daily_jewellery_full_discount_gross_profit.plot(kind='line', marker='>', label='珠宝首饰相关满减活动商品毛利润')
    
    plt.title('每日毛利润时间序列')
    plt.xlabel('日期')
    plt.ylabel('毛利润')
    plt.xticks(rotation=45)
    
    # 注释掉添加毛利润标注的代码
    # if show_labels:
    #     for x, y in zip(daily_gross_profit.index, daily_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,5), ha='center', fontsize=6)
    #     for x, y in zip(vip_daily_gross_profit.index, vip_daily_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,15), ha='center', fontsize=6)  # 拉开 VIP 标注距离
    #     for x, y in zip(normal_daily_gross_profit.index, normal_daily_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,5), ha='center', fontsize=6)
    #     for x, y in zip(daily_promo_gross_profit.index, daily_promo_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,25), ha='center', fontsize=6)  # 拉开促销商品标注距离
    #     for x, y in zip(normal_daily_promo_gross_profit.index, normal_daily_promo_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,35), ha='center', fontsize=6)  # 拉开普通用户促销毛利润标注距离
    #     for x, y in zip(daily_non_full_discount_gross_profit.index, daily_non_full_discount_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,45), ha='center', fontsize=6)  # 拉开非满减活动商品毛利润标注距离
    #     for x, y in zip(daily_jewellery_full_discount_gross_profit.index, daily_jewellery_full_discount_gross_profit):
    #         plt.annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(0,55), ha='center', fontsize=6)  # 拉开珠宝首饰相关满减活动商品毛利润标注距离
    
    plt.legend()
    plt.tight_layout()
    
    # 保存图片
    plt.savefig(os.path.join(output_folder, 'daily_gross_profit_distribution.png'))
    plt.close()

    # 绘制每日交易商品量和促销商品量时间序列图
    plt.figure(figsize=(12, 6))
    daily_product_count.plot(kind='line', marker='o', label='日交易商品量')
    daily_promo_product_count.plot(kind='line', marker='s', label='日促销商品量')
    daily_non_full_discount_product_count.plot(kind='line', marker='<', label='日非满减活动商品量')
    daily_jewellery_full_discount_product_count.plot(kind='line', marker='>', label='日珠宝首饰相关满减活动商品量')
    
    plt.title('每日交易商品量和促销商品量时间序列')
    plt.xlabel('日期')
    plt.ylabel('商品量')
    plt.xticks(rotation=45)
    
    # 保留商品量标注逻辑
    if show_labels:
        for x, y in zip(daily_product_count.index, daily_product_count):
            plt.annotate(f'{y:.0f}', (x, y), textcoords='offset points', xytext=(0,5), ha='center', fontsize=6)
        for x, y in zip(daily_promo_product_count.index, daily_promo_product_count):
            plt.annotate(f'{y:.0f}', (x, y), textcoords='offset points', xytext=(0,15), ha='center', fontsize=6)
        for x, y in zip(daily_non_full_discount_product_count.index, daily_non_full_discount_product_count):
            plt.annotate(f'{y:.0f}', (x, y), textcoords='offset points', xytext=(0,25), ha='center', fontsize=6)
        for x, y in zip(daily_jewellery_full_discount_product_count.index, daily_jewellery_full_discount_product_count):
            plt.annotate(f'{y:.0f}', (x, y), textcoords='offset points', xytext=(0,35), ha='center', fontsize=6)
    
    plt.legend()
    plt.tight_layout()
    
    # 保存图片
    plt.savefig(os.path.join(output_folder, 'daily_product_count_distribution.png'))
    plt.close()
    
    print(f"每日零售总额时间序列图已保存到 {output_folder} 文件夹")
    print(f"每日毛利润时间序列图已保存到 {output_folder} 文件夹")
    print(f"每日交易商品量和促销商品量时间序列图已保存到 {output_folder} 文件夹")
    print(f"每日零售、毛利润、交易商品量和促销商品量数据已保存到 {os.path.join(output_folder, 'daily_sales_data.xlsx')}")
    return sales_data

def plot_retail_price_distribution(df, output_folder='analysis_plots'):
    """
    统计零售价的分布情况，按照 VIP 和普通用户分类，零售价为负数的不用管，
    柱状图上标注该区间的中间价格和该区间占 VIP 用户/普通用户销售额的百分比
    :param df: 销售交易明细表数据框
    :param output_folder: 保存图片的文件夹路径，默认为 'analysis_plots'
    """
    # 定义该函数专用的 bins 数量
    RETAIL_PRICE_BINS = 20
    
    # 确保保存图片的文件夹存在
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # 设置图片清晰度
    plt.rcParams['figure.dpi'] = 300

    # 计算 RTLPC_C 加上折扣金额后的值
    df['调整后价格'] = df['RTLPC_C'] + df['折扣金额']

    # 筛选出调整后价格大于等于 0 的有效记录
    valid_df = df[df['调整后价格'] >= 0]

    # 筛选 VIP 和普通客户数据
    vip_df = valid_df[valid_df['VIP客户姓名'].notna()]
    normal_df = valid_df[valid_df['VIP客户姓名'].isna()]

    # 定义 3sigma 过滤函数
    def filter_by_3sigma(data):
        mean = data.mean()
        std = data.std()
        lower_bound = mean - 3 * std
        upper_bound = mean + 3 * std
        return data[(data >= lower_bound) & (data <= upper_bound)]
    
    # 过滤普通用户价格异常值
    normal_df_filtered = normal_df.copy()
    normal_df_filtered['调整后价格'] = filter_by_3sigma(normal_df['调整后价格'])

    # 计算 VIP 和普通用户的总销售额
    vip_total_sales = vip_df['调整后价格'].sum()
    normal_total_sales = normal_df_filtered['调整后价格'].sum()

    # 打印 VIP 和普通用户的零售价情况
    print("VIP 用户零售价情况：")
    print(vip_df['调整后价格'])
    print("普通用户零售价情况：")
    print(normal_df_filtered['调整后价格'])

    # 辅助函数：在直方图上添加标签（中间价格和百分比）
    def add_labels(ax, data, total_sales):
        n, bins, patches = ax.hist(data, bins=RETAIL_PRICE_BINS, edgecolor='black')
        for i in range(len(patches)):
            height = patches[i].get_height()
            bin_center = (bins[i] + bins[i+1]) / 2
            # 修正 bin_sales 计算逻辑，使用 Series 的布尔索引
            bin_sales = data[(data >= bins[i]) & (data < bins[i+1])].sum()
            percent = (bin_sales / total_sales * 100) if total_sales != 0 else 0
            if height > 0:
                label = f'{bin_center:.2f}\n{percent:.1f}%'
                ax.text(patches[i].get_x() + patches[i].get_width() / 2, height, label, 
                        ha='center', va='bottom', fontsize=8)

    # 绘制 VIP 用户零售价分布直方图
    plt.figure(figsize=(12, 6))
    ax1 = plt.gca()
    ax1.hist(vip_df['调整后价格'], bins=RETAIL_PRICE_BINS, edgecolor='black')
    plt.title('VIP用户零售价分布')
    plt.xlabel('零售价')
    plt.ylabel('数量')
    add_labels(ax1, vip_df['调整后价格'], vip_total_sales)
    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'vip_retail_price_distribution.png'))
    plt.close()

    # 绘制普通用户零售价分布直方图
    plt.figure(figsize=(12, 6))
    ax2 = plt.gca()
    ax2.hist(normal_df_filtered['调整后价格'], bins=RETAIL_PRICE_BINS, edgecolor='black')
    plt.title('普通用户零售价分布')
    plt.xlabel('零售价')
    plt.ylabel('数量')
    add_labels(ax2, normal_df_filtered['调整后价格'], normal_total_sales)
    plt.tight_layout()
    plt.savefig(os.path.join(output_folder, 'normal_retail_price_distribution.png'))
    plt.close()

    print(f"零售价分布图片已保存到 {output_folder} 文件夹")

overwrite = True
file_path = 'ch珠宝数据表.xlsx'  # 这里需要替换为实际文件路径
data_frames = read_excel_file(file_path)

if '销售交易明细表' in locals().get('data_frames', {}):

    df = data_frames['销售交易明细表']
    # 计算 RTLPC_C 加上折扣金额后的值
    df['调整后价格'] = df['RTLPC_C'] + df['折扣金额']
    vip_orders, normal_orders, order_diff, order_ratio, vip_sales, normal_sales, sales_diff, sales_ratio, total_profit_rate, vip_profit_rate, normal_profit_rate, gross_profit_ratio, vip_return_rate, normal_return_rate, vip_promo_orders, normal_promo_orders, vip_promo_rate, normal_promo_rate, vip_avg_consumption, normal_avg_consumption = calculate_order_stats(df)
    
    total_sales = df['调整后价格'].sum()

    template = "VIP用户交易单量: {}\n普通用户交易单量: {}\nVIP用户与普通用户交易单量差异: {}\n" \
                "VIP用户与普通用户交易单量比例: {:.2f}\n" \
                "总销售额: {}\nVIP用户零售总额: {}\n普通用户零售总额: {}\nVIP用户与普通用户零售总额差异: {}\n" \
                "VIP用户与普通用户零售总额比例: {:.2f}\n" \
                "总利润率: {:.2%}\nVIP用户利润率: {:.2%}\n普通用户利润率: {:.2%}\n" \
                "VIP用户与普通用户毛利润比例: {:.2f}\n" \
                "VIP用户退货率: {:.2%}\n普通用户退货率: {:.2%}\n" \
                "VIP用户促销相关单量: {}\n普通用户促销相关单量: {}\n" \
                "VIP用户促销相关率: {:.2%}\n普通用户促销相关率: {:.2%}\n" \
                "VIP用户平均每单消费: {:.2f}\n普通用户平均每单消费: {:.2f}\n"

    print(template.format(vip_orders, normal_orders, order_diff, order_ratio, total_sales, vip_sales, normal_sales, sales_diff, sales_ratio, total_profit_rate, vip_profit_rate, normal_profit_rate, gross_profit_ratio, vip_return_rate, normal_return_rate, vip_promo_orders, normal_promo_orders, vip_promo_rate, normal_promo_rate, vip_avg_consumption, normal_avg_consumption))
    save_results_to_txt(vip_orders, normal_orders, order_diff, order_ratio, total_sales, vip_sales, normal_sales, sales_diff, sales_ratio, total_profit_rate, vip_profit_rate, normal_profit_rate, gross_profit_ratio, vip_return_rate, normal_return_rate, vip_promo_orders, normal_promo_orders, vip_promo_rate, normal_promo_rate, vip_avg_consumption, normal_avg_consumption, template=template, overwrite=True)
    
    # 统计并绘制物料类型分布
    plot_material_distribution(df)
    
    # 统计并绘制消费分布
    plot_order_consumption_distribution(df)
    
    # 统计并绘制每日零售总额和毛利润时间序列
    daily_sales = plot_daily_sales(df)

    # 统计并绘制零售价分布
    plot_retail_price_distribution(df)
else:
    print("未找到 '销售交易明细表' 工作表，请检查文件。")
