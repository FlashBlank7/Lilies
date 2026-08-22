import pandas as pd
import os
import numpy as np
from scipy.signal import butter, filtfilt
import pandas as pd
import matplotlib.pyplot as plt  # 补充缺失的导入
import matplotlib.dates as mdates
import logging
# 设置 matplotlib 的字体和负号显示
plt.rcParams['font.sans-serif'] = ['SimHei']  # 使用黑体字体
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

# 配置日志记录
logging.basicConfig(
    filename='data_processing.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filemode='w'
)
def log_print(message):
    """将信息同时输出到日志文件和控制台"""
    print(message)
    logging.info(message)
# 读取采集数据并进行滤波，积分处理
class DataProcessor:
    time_col:str
    def __init__(self):
        # pass
        self.file_contents = []
        # 记录每个文件已完成的处理步骤
        self.processed_status = {} 
        self.time_col = 'time_sec'
        # 初始化速度和位移列名
        self.ax_col = 'AX'
        self.ay_col = 'AY'
        self.vx_col = 'VX'
        self.vy_col = 'VY'
        self.dx_col = 'DX'
        self.dy_col = 'DY'
        # 新增列名，用于存储新积分策略的结果
        self.vy_new_col = 'VY_new'
        self.dy_new_col = 'DY_new'
        # 定义重力加速度常量
        # self.GRAVITY = 0.9806650
        self.GRAVITY = 0
    def add_file(self,file_path):
        
        if os.path.isdir(file_path):
            for root, _, files in os.walk(file_path):
                for file in files:
                    if file.endswith('.xlsx') or file.endswith('.xls'):
                        full_path = os.path.join(root, file)
                        try:
                            content = pd.read_excel(full_path)
                            self.file_contents.append({'filename': file, 'content': content})
                            if file not in self.processed_status:
                                self.processed_status[file] = {
                                    'date_time_tran': False, 
                                    'clean_data': False, 
                                    'add_time_column': False, 
                                    'calc_velocity_and_displacement': False,
                                    'calc_velocity_and_displacement_new': False,  # 新增处理步骤
                                    'exclude_gravity':False,
                                    'filtered':False,
                                    'subtract_pause_mean': False  
                                        }
                        except Exception as e:
                            log_print(f"读取文件 {full_path} 时出错: {e}")
        else:
            if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                try:
                    content = pd.read_excel(file_path)
                    self.file_contents.append({'filename': os.path.basename(file_path), 'content': content})
                    if os.path.basename(file_path) not in self.processed_status:
                        self.processed_status[os.path.basename(file_path)] = {
                            'date_time_tran': False, 
                            'clean_data': False, 
                            'add_time_column': False, 
                            'calc_velocity_and_displacement': False,
                            'calc_velocity_and_displacement_new': False,  # 新增处理步骤
                            'exclude_gravity':False,
                            'filtered':False,
                            'subtract_pause_mean': False 
                                }
                except Exception as e:
                    log_print(f"读取文件 {file_path} 时出错: {e}")
        
    def clean_data(self,df,filename):
        original_length = len(df)  # 记录原始数据长度
        
        # 删除遇到第一个status为“开”之前的字段
        if 'status' in df.columns:
            first_open_index = df[df['status'] == '开'].index.min()
            if pd.notna(first_open_index):
                df = df.loc[first_open_index:]
        
        # 删除最后一个“关”之后的所有字段
        if 'status' in df.columns:
            last_close_index = df[df['status'] == '关'].index.max()
            if pd.notna(last_close_index):
                df = df.loc[:last_close_index]
        
        # 统计并打印删除的字段数量
        deleted_count = original_length - len(df)
        log_print(f"文件{filename}删除了{deleted_count}行数据")
        if 'create_dt' in df.columns:          
            # 删除时间列中的异常值
            df = df.dropna(subset=['create_dt'])
            
        # 检查加速度和角速度列
        accel_columns = ['AX', 'AY', 'AZ']
        gyro_columns = ['GX', 'GY', 'GZ']
        analysis_columns = accel_columns + gyro_columns
        
        # 确保这些列存在
        for col in analysis_columns:
            if col not in df.columns:
                log_print(f"对暂停状态的参数进行统计时，发现文件{filename}列{col}缺失")
                # 如果缺少某些列，创建它们并填充为0
                df[col] = 0.0
        
        # 对电梯暂停时的参数进行统计分析
        pause_stats = pd.DataFrame()
        if 'status' in df.columns:
            open_indices = df[df['status'] == '开'].index
            close_indices = df[df['status'] == '关'].index
            
            # 找到每一对“开”和“关”的区间
            for open_idx in open_indices:
                next_close_idx = close_indices[close_indices > open_idx].min()
                if pd.notna(next_close_idx):
                    pause_data = df.loc[open_idx:next_close_idx]
                    pause_stats = pd.concat([pause_stats, pause_data])
        
        # 删除加速度和角速度列的异常值
        df = df.dropna(subset=analysis_columns)
        
        # 返回处理后的数据和统计结果
        return df, pause_stats
    # def data_process_with_flat_curve(self):
        
    #     for item in self.file_contents:
    #         filename = item['filename']
    #         if "flat_curve" not in self.processed_status[filename].columns:
    #             self.processed_status[filename]["flat_curve"] = False
    #             # 进行曲线平滑处理
    #             item['content'] = self.fil(item['content'])
    #             self.processed_status[filename]["flat_curve"] = True
    #         elif self.processed_status[filename]["flat_curve"]:
    #             print(f"文件{filename}已进行曲线平滑处理过")
    # def flat_curve(self,df):
    #     # 对加速度和角速度列进行曲线平滑处理
    #     for col in ['AX', 'AY', 'AZ', 'GX', 'GY', 'GZ']:
    #         df[col] = df[col].rolling(window=2, min_periods=1).mean()
    #     return df
        
    def data_process(self):
        if not self.file_contents:
            log_print("没有文件内容可处理")
            return
        # 定义处理步骤映射
        process_steps = {
            'date_time_tran': {
                'method': self.date_time_tran,
                'error_msg': "日期数据处理过程中出错"
            },
            'clean_data': {
                'method': lambda item: self.clean_data(item['content'], item['filename']),
                'error_msg': "数据清洁过程中出错",
                'update_content': True
            },
            'add_time_column': {
                'method': self.add_time_column,
                'error_msg': "添加时间列过程中出错"
            },
            'calc_velocity_and_displacement': {
                'method': self.calc_velocity_and_displacement,
                'error_msg': "计算速度和位移过程中出错"
            }
        }
        # 遍历每个处理步骤
        for step_name, step_info in process_steps.items():
            if step_name == 'calc_velocity_and_displacement':
                continue
            try:
                if any(not self.processed_status[item['filename']][step_name] for item in self.file_contents):
                    if step_info.get('update_content'):
                        for item in self.file_contents:
                            if not self.processed_status[item['filename']][step_name]:
                                log_print(f"{item['filename']}文件正在进行{step_name}处理")
                                item['content'],item['pause_status'] = step_info['method'](item)
                                self.processed_status[item['filename']][step_name] = True
                            else:
                                log_print(f"{item['filename']}文件已经进行{step_name}处理")
                    else:
                        log_print(f"执行{step_name}处理")
                        step_info['method']()
                        for item in self.file_contents:
                            if not self.processed_status[item['filename']][step_name]:
                                log_print(f"{item['filename']}文件正在进行{step_name}处理")
                                self.processed_status[item['filename']][step_name] = True
                            else:
                                log_print(f"{item['filename']}文件已经进行{step_name}处理")
            except Exception as e:
                log_print(f"{step_info['error_msg']}: {e}")
        # for file_data in self.file_contents:
        #     if self.processed_status[file_data['filename']]['exclude_gravity']:
        #         log_print(f"{file_data['filename']}文件已经进行exclude_gravity处理")
        #         continue
        #     log_print(f"{file_data['filename']}文件正在进行exclude_gravity处理")
        #     df = file_data['content']
        #     # 减去重力加速度对Y轴加速度的影响
        #     df['AY'] = df['AY'] - self.GRAVITY
        #     file_data['content'] = df
        #     self.processed_status[file_data['filename']]['exclude_gravity'] = True
    def data_process_with_filter_without_minus_means(self):
        self.data_process()
        for item in self.file_contents:
            if not self.processed_status[item['filename']]['filtered']:
                log_print(f"{item['filename']}文件正在进行filtered处理")
                self.filter_data(item)
                self.processed_status[item['filename']]['filtered'] = True
            else:
                log_print(f"{item['filename']}文件已经进行filtered处理")

    def data_process_with_filter(self):
        self.data_process()
        for item in self.file_contents:
            if not self.processed_status[item['filename']]['filtered']:
                log_print(f"{item['filename']}文件正在进行filtered处理")
                self.filter_data(item)
                self.processed_status[item['filename']]['filtered'] = True
            else:
                log_print(f"{item['filename']}文件已经进行filtered处理")
        # 减去电梯暂停时数据的均值
        for item in self.file_contents:
            if not self.processed_status[item['filename']]['subtract_pause_mean']:
                try:
                    log_print(f"{item['filename']}文件正在进行subtract_pause_mean处理")
                    df = item['content']
                    if 'pause_status' in item:
                        pause_stats = item['pause_status']
                        if not pause_stats.empty:
                            # 仅对 AY 减去暂停时的均值
                            if 'AY' in df.columns:
                                df['AY'] -= pause_stats['AY'].mean()
                    
                    # 对 AX 减去非暂停时的均值
                    if 'AX' in df.columns and 'pause_status' in item and not item['pause_status'].empty:
                        # 获取暂停状态的索引
                        pause_indices = item['pause_status'].index
                        # 获取非暂停状态的数据
                        non_pause_data = df.drop(pause_indices)
                        # 计算非暂停状态下 AX 的均值
                        non_pause_mean = non_pause_data['AX'].mean()
                        df['AX'] -= non_pause_mean
                    elif 'AX' in df.columns:
                        # 如果没有暂停数据，直接使用全部数据的均值
                        df['AX'] -= df['AX'].mean()
                    item['content'] = df
                    self.processed_status[item['filename']]['subtract_pause_mean'] = True
                except Exception as e:
                    log_print(f"减去电梯暂停时数据均值过程中出错: {e}")
            else:
                log_print(f"{item['filename']}文件已经进行subtract_pause_mean处理")
                
    def filter_data(self,item):
        # 定义截止频率和采样频率
        cutoff_freq = 1  # 截止频率，单位Hz
        sampling_freq = 5  # 采样频率，单位Hz
        order = 2  # 滤波器阶数
        # 计算归一化截止频率
        nyquist_freq = 0.5 * sampling_freq
        normalized_cutoff = cutoff_freq / nyquist_freq
        # 设计二阶巴特沃斯滤波器
        b, a = butter(N=order, Wn=normalized_cutoff, btype='low', analog=False)
        # 对每个文件中的加速度和角速度数据进行滤波
        accel_columns = ['AX', 'AY', 'AZ']
        gyro_columns = ['GX', 'GY', 'GZ']
        df = item['content']
        for col in accel_columns + gyro_columns:
            if col in df.columns:
                df[col] = filtfilt(b, a, df[col])
        item['content'] = df
            
    def date_time_tran(self):
        for item in self.file_contents:
            if 'create_dt' in item['content'].columns:
                item['content']['create_dt'] = pd.to_datetime(item['content']['create_dt'], format=r'%Y-%m-%d %H:%M:%S')
                # 去掉日期部分，只保留时间
                # 修改此处，不转换为 time 类型，以便后续绘图使用
                # item['content']['create_dt'] = item['content']['create_dt'].dt.time
            else:
                log_print(f"列表{item['filename']}中不存在create_dt列")    
    def add_time_column(self):
        """
        为每个文件的数据添加一个新的时间列，以秒为单位，第一个数据是0秒，第二个是0.2秒，第三是0.4秒，以此类推
        """
        for item in self.file_contents:
            df = item['content']
            df['time_sec'] = [i * 0.2 for i in range(len(df))]
            item['content'] = df
    def calc_velocity_and_displacement(self):
        """
        根据AX，AY计算并添加对应的X轴速度，Y轴速度；X轴位移，Y轴位移列
        对于X轴，在电梯非暂停状态，认为加速度是0；对于Y轴，在电梯暂停状态，认为加速度是0
        """
        for item in self.file_contents:
            df = item['content']
            if 'AX' in df.columns and 'AY' in df.columns and 'status' in df.columns:
                log_print(f"{item['filename']}文件正在进行calc_velocity_and_displacement处理")
                # 初始化速度和位移列
                df[self.vx_col] = 0.0
                df[self.vy_col] = 0.0
                df[self.dx_col] = 0.0
                df[self.dy_col] = 0.0
                # 计算时间间隔
                dt = df[self.time_col].diff()
                dt.iloc[0] = 0.2  # 第一个时间间隔设为0.2秒
                # 创建暂停状态掩码
                open_indices = df[df['status'] == '开'].index
                close_indices = df[df['status'] == '关'].index
                pause_mask = pd.Series(False, index=df.index)
                for open_idx in open_indices:
                    next_close_idx = close_indices[close_indices > open_idx].min()
                    if pd.notna(next_close_idx):
                        pause_mask.loc[open_idx:next_close_idx] = True
                # 创建X轴和Y轴加速度序列
                ax = df['AX'].copy()
                ay = df['AY'].copy()
                # 对于X轴，在非暂停状态加速度设为0
                ax[~pause_mask] = 0
                # 对于Y轴，在暂停状态加速度设为0
                ay[pause_mask] = 0

                # 初始化速度和位移序列
                vx = pd.Series(0.0, index=df.index)
                vy = pd.Series(0.0, index=df.index)
                dx = pd.Series(0.0, index=df.index)
                dy = pd.Series(0.0, index=df.index)

                # 用于记录楼层对应的位移值
                floor_displacement = {}

                # 找到所有暂停和非暂停区间
                intervals = []
                prev_mask = pause_mask.iloc[0]
                start_idx = df.index[0]
                for idx in df.index[1:]:
                    current_mask = pause_mask.loc[idx]
                    if current_mask != prev_mask:
                        intervals.append((start_idx, idx - 1, prev_mask))
                        start_idx = idx
                        prev_mask = current_mask
                intervals.append((start_idx, df.index[-1], prev_mask))

                # 处理每个区间
                for start, end, is_pause in intervals:
                    interval_slice = slice(start, end)
                    interval_dt = dt.loc[interval_slice]
                    # 计算X轴速度
                    if is_pause:
                        interval_ax = ax.loc[interval_slice]
                        vx.loc[interval_slice] = (interval_ax * interval_dt).cumsum()
                        # 计算X轴位移
                        interval_vx = vx.loc[interval_slice]
                        dx.loc[interval_slice] = (interval_vx * interval_dt).cumsum()
                    else:
                        # 非暂停状态X轴位移归零
                        dx.loc[interval_slice] = 0
                    # 计算Y轴速度
                    interval_ay = ay.loc[interval_slice]
                    vy.loc[interval_slice] = (interval_ay * interval_dt).cumsum()

                    # 计算Y轴位移
                    if is_pause:
                        # 检查当前区间是否有楼层信息
                        if 'floor' in df.columns:
                            # 获取当前区间的第一个点的楼层
                            floor_value = df.loc[start, 'floor']
                            if pd.notna(floor_value):
                                if floor_value in floor_displacement:
                                    # 如果当前楼层在之前已经出现过，则继承之前楼层对应的位移值
                                    dy.loc[interval_slice] = floor_displacement[floor_value]
                                else:
                                    # 如果未出现过，则位移初始值继承进入暂停状态的位移值，并为当前楼层登记对应的位移
                                    if start > df.index[0]:
                                        dy.loc[interval_slice] = dy.loc[start - 1]
                                        floor_displacement[floor_value] = dy.loc[start - 1]
                                    else:
                                        dy.loc[interval_slice] = 0
                                        floor_displacement[floor_value] = 0
                            else:
                                # 如果没有楼层信息，保持原逻辑
                                if start > df.index[0]:
                                    dy.loc[interval_slice] = dy.loc[start - 1]
                        else:
                            # 如果没有floor列，保持原逻辑
                            if start > df.index[0]:
                                dy.loc[interval_slice] = dy.loc[start - 1]
                    else:
                        # 非暂停状态Y轴位移从进入非暂停状态的值开始积分
                        if start > df.index[0]:
                            prev_dy = dy.loc[start - 1]
                        else:
                            prev_dy = 0
                        interval_vy = vy.loc[interval_slice]
                        dy.loc[interval_slice] = prev_dy + (interval_vy * interval_dt).cumsum()

                df[self.vx_col] = vx
                df[self.vy_col] = vy
                df[self.dx_col] = dx
                df[self.dy_col] = dy
                item['content'] = df
                self.processed_status[item['filename']]['calc_velocity_and_displacement'] = True
            elif 'status' not in df.columns:
                log_print(f"文件{item['filename']}缺少status列，无法确定暂停状态，无法计算速度和位移")
            else:
                log_print(f"文件{item['filename']}缺少AX或AY列，无法计算速度和位移")
    def calc_velocity_and_displacement_new(self):
        """
        采用另一种积分策略，对于X轴，在电梯非暂停状态，不抹零加速度；
        对于Y轴，在电梯暂停状态，认为加速度是0；
        积分得到Y轴速度后，将Y轴速度在暂停状态的速度抹为0，再积分得到Y轴位移
        """        
        for item in self.file_contents:
            df = item['content']
            if 'AX' in df.columns and 'AY' in df.columns and 'status' in df.columns:
                log_print(f"{item['filename']}文件正在进行calc_velocity_and_displacement_new处理")
                # 初始化新的Y轴速度和位移列
                df[self.vy_new_col] = 0.0
                df[self.dy_new_col] = 0.0
                # 初始化X轴速度和位移列
                df[self.vx_col] = 0.0
                df[self.dx_col] = 0.0
                # 计算时间间隔
                dt = df[self.time_col].diff()
                dt.iloc[0] = 0.2  # 第一个时间间隔设为0.2秒
                # 创建暂停状态掩码
                open_indices = df[df['status'] == '开'].index
                close_indices = df[df['status'] == '关'].index
                pause_mask = pd.Series(False, index=df.index)
                for open_idx in open_indices:
                    next_close_idx = close_indices[close_indices > open_idx].min()
                    if pd.notna(next_close_idx):
                        pause_mask.loc[open_idx:next_close_idx] = True
                # 创建X轴和Y轴加速度序列
                ax = df['AX'].copy()
                ay = df['AY'].copy()
                # 对于X轴，不抹零加速度
                # 对于Y轴，在暂停状态加速度设为0
                ay[pause_mask] = 0

                # 初始化速度和位移序列
                vx = pd.Series(0.0, index=df.index)
                vy = pd.Series(0.0, index=df.index)
                dx = pd.Series(0.0, index=df.index)
                dy = pd.Series(0.0, index=df.index)

                # 用于记录楼层对应的位移值
                floor_displacement = {}

                # 找到所有暂停和非暂停区间
                intervals = []
                prev_mask = pause_mask.iloc[0]
                start_idx = df.index[0]
                for idx in df.index[1:]:
                    current_mask = pause_mask.loc[idx]
                    if current_mask != prev_mask:
                        intervals.append((start_idx, idx - 1, prev_mask))
                        start_idx = idx
                        prev_mask = current_mask
                intervals.append((start_idx, df.index[-1], prev_mask))

                # 处理每个区间
                for start, end, is_pause in intervals:
                    interval_slice = slice(start, end)
                    interval_dt = dt.loc[interval_slice]
                    # 计算X轴速度
                    interval_ax = ax.loc[interval_slice]
                    vx.loc[interval_slice] = (interval_ax * interval_dt).cumsum()

                    # 计算X轴位移
                    if is_pause:
                        interval_vx = vx.loc[interval_slice]
                        dx.loc[interval_slice] = (interval_vx * interval_dt).cumsum()
                    else:
                        # 非暂停状态X轴位移归零
                        dx.loc[interval_slice] = 0

                    # 计算Y轴速度
                    interval_ay = ay.loc[interval_slice]
                    vy.loc[interval_slice] = (interval_ay * interval_dt).cumsum()

                    # 计算Y轴位移
                    if is_pause:
                        # 检查当前区间是否有楼层信息
                        if 'floor' in df.columns:
                            # 获取当前区间的第一个点的楼层
                            floor_value = df.loc[start, 'floor']
                            if pd.notna(floor_value):
                                if floor_value in floor_displacement:
                                    # 如果当前楼层在之前已经出现过，则继承之前楼层对应的位移值
                                    dy.loc[interval_slice] = floor_displacement[floor_value]
                                else:
                                    # 如果未出现过，则位移初始值继承进入暂停状态的位移值，并为当前楼层登记对应的位移
                                    if start > df.index[0]:
                                        dy.loc[interval_slice] = dy.loc[start - 1]
                                        floor_displacement[floor_value] = dy.loc[start - 1]
                                    else:
                                        dy.loc[interval_slice] = 0
                                        floor_displacement[floor_value] = 0
                            else:
                                # 如果没有楼层信息，保持原逻辑
                                if start > df.index[0]:
                                    dy.loc[interval_slice] = dy.loc[start - 1]
                        else:
                            # 如果没有floor列，保持原逻辑
                            if start > df.index[0]:
                                dy.loc[interval_slice] = dy.loc[start - 1]
                    else:
                        # 非暂停状态Y轴位移从进入非暂停状态的值开始积分
                        if start > df.index[0]:
                            prev_dy = dy.loc[start - 1]
                        else:
                            prev_dy = 0
                        interval_vy = vy.loc[interval_slice]
                        dy.loc[interval_slice] = prev_dy + (interval_vy * interval_dt).cumsum()

                df[self.vx_col] = vx
                df[self.vy_col] = vy
                df[self.dx_col] = dx
                df[self.dy_col] = dy
                item['content'] = df
                self.processed_status[item['filename']]['calc_velocity_and_displacement_new'] = True
            elif 'status' not in df.columns:
                log_print(f"文件{item['filename']}缺少status列，无法确定暂停状态，无法使用新策略计算速度和位移")
            else:
                log_print(f"文件{item['filename']}缺少AX或AY列，无法使用新策略计算速度和位移")
    @property
    def data(self):
        return self.file_contents
class Ploter:
    exclude_cols: list = None
    # 定义需要绘制的特定列，使用 processor 中的成员变量名
    def __init__(self,data_processor):
        self.exclude_cols = ['tag',"status"]
        self.dataprocessor =  data_processor
        self.target_col_map = {
            'X轴加速度': self.dataprocessor.ax_col,
            'X轴速度': self.dataprocessor.vx_col,
            'X轴位移': self.dataprocessor.dx_col,
            'Y轴加速度': self.dataprocessor.ay_col,
            'Y轴速度': self.dataprocessor.vy_col,
            'Y轴位移': self.dataprocessor.dy_col
        }


    def plot_curves(self, save_folder='./normal_plots'):
        """
        对每个文件单独进行正常运行曲线绘制（X轴加速度、X轴速度、X轴位移； Y轴加速度、Y轴速度、Y轴位移），
        并对每个文件保存对应最终结果
        :param save_folder: 保存图片的文件夹路径，默认为 './normal_plots'
        """
        # 确保保存文件夹存在
        os.makedirs(save_folder, exist_ok=True)
        

        # 获取所有文件数据
        file_contents = self.dataprocessor.data
        
        for item in file_contents:
            filename = item['filename']
            df = item['content']
            # 检查是否存在 create_dt 列
            if 'create_dt' not in df.columns:
                log_print(f"文件 {filename} 缺少 create_dt 列，无法以现实时间绘制曲线")
                continue

            # 筛选出目标列
            value_cols = []
            display_names = []
            for display_name, col in self.target_col_map.items():
                if col in df.columns:
                    value_cols.append(col)
                    display_names.append(display_name)
            num_plots = len(value_cols)
            #
            fig, axes = plt.subplots(num_plots, 1, figsize=(10, 6 * num_plots))
            if num_plots == 1:
                axes = [axes]  # 确保axes始终是列表形式
            
            # 遍历目标列，在不同子图上绘制曲线
            for i, (col, display_name) in enumerate(zip(value_cols, display_names)):
                # plot_data = df[col]
                # 如果是加速度列，将数据乘10转换为 m/s² 单位
                if '加速度' in display_name:
                    plot_data = df[col] * 10
                    axes[i].set_ylabel('数值 (m/s²)')
                elif '速度' in display_name:
                    plot_data = df[col] * 10
                    axes[i].set_ylabel('数值 (m/s)')
                elif '位移' in display_name:
                    plot_data = df[col] * 10
                    axes[i].set_ylabel('数值 (m)')
                else:
                    plot_data = df[col]
                    axes[i].set_ylabel('数值')
                axes[i].plot(df['create_dt'], plot_data, label=display_name)
                axes[i].set_xlabel('时间')
                axes[i].set_title(f'{filename} - {display_name} 时间序列曲线')
                axes[i].legend()
                axes[i].grid(True)

                # 设置 x 轴为每 1 分钟一个刻度
                axes[i].xaxis.set_major_locator(mdates.MinuteLocator(interval=1))
                axes[i].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
                plt.setp(axes[i].get_xticklabels(), rotation=45)

                # 如果是Y轴位移图，在status为“开”或“关”且floor列存在的点标注楼层
                if display_name == 'Y轴位移' and 'floor' in df.columns and 'status' in df.columns:
                    floor_points = df[(df['status'].isin(['开'])) & df['floor'].notna()]
                    for idx, row in floor_points.iterrows():
                        axes[i].annotate(
                            str(row['floor']),
                            xy=(row['create_dt'], row[col] * 10),
                            xytext=(5, 5),
                            textcoords='offset points',
                            arrowprops=dict(arrowstyle='->')
                        )
            
            plt.tight_layout()
            
            # 保存图片到指定文件夹
            base_name = os.path.splitext(filename)[0]
            save_path = os.path.join(save_folder, f'{base_name}_normal_curves.png')
            plt.savefig(save_path)
            plt.close()  # 关闭当前图形，避免内存泄漏

if __name__ == "__main__":
    file_path = "正常数据集"

    normal_dataprocessor = DataProcessor()
    normal_dataprocessor.add_file(file_path)
    normal_dataprocessor.data_process_with_filter()
    normal_dataprocessor.calc_velocity_and_displacement()
    # normal_datas = normal_dataprocessor.data

    normal_ploter = Ploter(normal_dataprocessor)

    normal_ploter.plot_curves(r"normal_plots")


