import json
from this import d
import warnings
import pandas as pd
import os
import numpy as np
from scipy import stats
from scipy.signal import welch
from data_process_and_plot import DataProcessor, log_print
import json
import warnings


# 确保中文显示
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

class FeatureExtractor:
    """特征提取器类，用于从数据中提取时域、频域和非线性动力学特征"""
    
    def __init__(self):
        # 定义要提取特征的列
        self.signal_columns = ['AX', 'AY', 'AZ', 'GX', 'GY', 'GZ']
    
    def normalize(self, data):
        """对数据进行组内归一化（减去均值，除以标准差）"""
        normalized_data = data.copy()
        for col in self.signal_columns:
            if col in normalized_data.columns:
                mean_val = normalized_data[col].mean()
                std_val = normalized_data[col].std()
                if std_val > 0:
                    normalized_data[col] = (normalized_data[col] - mean_val) / std_val
        return normalized_data
    
    def extract_time_domain_features(self, signal):
        """提取时域特征"""
        features = []
        
        # 均值
        features.append(np.mean(signal))
        # 标准差
        features.append(np.std(signal))
        # 均方根（RMS）
        features.append(np.sqrt(np.mean(signal**2)))
        # 峰值
        features.append(np.max(np.abs(signal)))
        # 偏度
        features.append(stats.skew(signal))
        # 峰度
        features.append(stats.kurtosis(signal))
        # 波形因子（RMS除以绝对值的平均值）
        abs_mean = np.mean(np.abs(signal))
        if abs_mean > 0:
            features.append(np.sqrt(np.mean(signal**2)) / abs_mean)
        else:
            features.append(0)

        # 脉冲因子（峰值除以绝对值的平均值）
        if abs_mean > 0:
            features.append(np.max(np.abs(signal)) / abs_mean)
        else:
            features.append(0)
        # 裕度因子（峰值除以RMS）
        rms = np.sqrt(np.mean(signal**2))
        if rms > 0:
            features.append(np.max(np.abs(signal)) / rms)
        else:
            features.append(0)
        # 绝对值的平均值
        features.append(abs_mean)
        
        return features
    
    def extract_frequency_domain_features(self, signal, fs=5):
        """提取频域特征"""
        features = []
        
        # 使用Welch方法计算功率谱密度
        f, Pxx = welch(signal, fs=fs, nperseg=min(1024, len(signal)), noverlap=min(512, len(signal)//2))
        
        # 频谱峰值
        features.append(np.max(Pxx))
        
        # 频谱质心
        if np.sum(Pxx) > 0:
            centroid_freq = np.sum(f * Pxx) / np.sum(Pxx)
        else:
            centroid_freq = 0
        features.append(centroid_freq)
        
        # 均方频率
        if np.sum(Pxx) > 0:
            mean_square_freq = np.sum(f**2 * Pxx) / np.sum(Pxx)
        else:
            mean_square_freq = 0
        features.append(mean_square_freq)
        
        # 频率方差
        if np.sum(Pxx) > 0:
            freq_variance = np.sum((f - centroid_freq)**2 * Pxx) / np.sum(Pxx)
        else:
            freq_variance = 0
        features.append(freq_variance)
        
        # 频谱熵
        if np.sum(Pxx) > 0:
            normalized_Pxx = Pxx / np.sum(Pxx)
            # 修复弃用警告，使用列表推导式并过滤掉可能的非正值
            spectral_entropy = -sum(p * np.log2(p) for p in normalized_Pxx if p > 0)
        else:
            spectral_entropy = 0
        features.append(spectral_entropy)
        
        return features
    
    def spectral_entropy(self, x):
        """计算频谱熵"""
        try:
            # 计算FFT
            fft_values = np.fft.fft(x)
            # 获取频谱幅值的平方作为功率谱
            power_spectrum = np.abs(fft_values[:len(fft_values)//2])**2
            
            # 计算归一化功率谱
            total_power = np.sum(power_spectrum)
            if total_power == 0:
                return 0
            
            probs = power_spectrum / total_power
            
            # 计算熵（修复弃用警告）
            entropy = -sum(p * np.log2(p) for p in probs if p > 0)
            
            # 确保返回有效数值
            return float(entropy) if np.isfinite(entropy) else 0
        except Exception as e:
            log_print(f"计算频谱熵时出错: {e}")
            return 0
            
    def sample_entropy(self, x, m=2, r=0.2):
        """计算样本熵"""
        n = len(x)
        r = r * np.std(x)
        
        def _maxdist(x_i, x_j):
            return np.max(np.abs(x_i - x_j))
        
        def _phi(m_val):
            data = np.array(x)
            C = {}
            for i in range(n - m_val + 1):
                x_i = data[i:i+m_val]
                count = 0
                for j in range(n - m_val + 1):
                    if i != j:
                        x_j = data[j:j+m_val]
                        d = _maxdist(x_i, x_j)
                        if d <= r:
                            count += 1
                if count > 0:
                    C[i] = count
            if len(C) == 0 or (n - m_val + 1) <= 1:
                return 1e-10  # 返回一个很小的值避免除零错误
            return np.sum(list(C.values())) / ((n - m_val + 1) * (n - m_val))
        
        try:
            phi_m = _phi(m)
            phi_m_plus_1 = _phi(m+1)
            if phi_m > 0 and phi_m_plus_1 > 0:
                return -np.log(phi_m_plus_1 / phi_m)
            else:
                return 0
        except Exception as e:
            log_print(f"计算样本熵时出错: {e}")
            return 0
    
    def permutation_entropy(self, x, m=3, delay=1):
        """计算排列熵"""
        n = len(x)
        permutations = []
        
        for i in range(n - m*delay + 1):
            # 取延迟嵌入向量
            embedded = x[i:i+m*delay:delay]
            # 获取排序后的索引位置
            permutation = np.argsort(embedded)
            permutations.append(tuple(permutation))
        
        # 计算各排列的概率
        from collections import Counter
        perm_counts = Counter(permutations)
        probs = [count/len(permutations) for count in perm_counts.values()]
        
        # 计算熵
        entropy = -sum(p * np.log2(p) for p in probs)
        # 归一化
        max_entropy = np.log2(np.math.factorial(m))
        return entropy / max_entropy if max_entropy > 0 else 0
    
    def extract_nonlinear_features(self, signal):
        """提取非线性动力学特征"""
        features = []
        
        # 样本熵
        features.append(self.sample_entropy(signal))
        # 排列熵
        features.append(self.permutation_entropy(signal))
        
        # # 填充3个0以达到5个特征（与其他域保持一致）
        # features.extend([0, 0, 0])
        
        return features
    
    def extract_features(self, data):
        """从数据中提取所有特征"""
        all_features = []
        
        for col in self.signal_columns:
            if col in data.columns:
                signal = data[col].values
                
                # 提取时域特征
                time_features = self.extract_time_domain_features(signal)
                
                # 提取频域特征
                freq_features = self.extract_frequency_domain_features(signal)
                
                # 提取非线性动力学特征
                nonlinear_features = self.extract_nonlinear_features(signal)
                
                # 合并特征
                all_features.extend(time_features + freq_features + nonlinear_features)
        
        # # 确保特征长度为90
        # if len(all_features) < 90:
        #     all_features.extend([0] * (90 - len(all_features)))
        # elif len(all_features) > 90:
        #     all_features = all_features[:90]
        
        return all_features

def process_abnormal_data(abnormal_dir, output_file):
    """处理故障数据"""
    log_print("开始处理故障数据...")
    
    # 创建数据处理器
    abnormal_processor = DataProcessor()
    abnormal_processor.add_file(abnormal_dir)
    
    # 调用filter处理数据
    abnormal_processor.data_process_with_filter_without_minus_means()
    
    # 创建特征提取器
    feature_extractor = FeatureExtractor()
    
    # 存储处理后的数据集
    dataset = []
    
    # 处理每个文件
    for item in abnormal_processor.file_contents:
        filename = item['filename']
        log_print(f"处理故障文件: {filename}")
        
        df = item['content'].copy()
        
        # 提取故障数据组
        fault_groups = []
        
        try:
            if 'status' in df.columns:
                # 找出status非空的所有行
                non_null_status_mask = df['status'].notna()
                non_null_indices = df[non_null_status_mask].index
                
                if not non_null_indices.empty:
                    # 根据索引的连续性将数据分成组
                    groups = []
                    current_group = [non_null_indices[0]]
                    
                    for idx in non_null_indices[1:]:
                        if idx == current_group[-1] + 1:
                            current_group.append(idx)
                        else:
                            groups.append(current_group)
                            current_group = [idx]
                    groups.append(current_group)
                    
                    # 提取每组数据
                    for group_indices in groups:
                        group_data = df.loc[group_indices]
                        # if len(group_data) > 10:  # 只保留数据点足够的组
                        fault_groups.append(group_data)
                
                # 如果通过status没有提取到足够的数据组，使用备选策略
                if len(fault_groups) == 0 and len(df) > 0:
                    # 将整个文件作为一个故障组
                    fault_groups = [df]
                    log_print(f"{filename}: status列存在但无有效数据，使用整个文件作为故障组")
            else:
                # 没有status列，将整个文件作为一个故障组
                fault_groups = [df]
                log_print(f"{filename}: 未找到status列，使用整个文件作为故障组")
        except Exception as e:
            log_print(f"{filename}: 提取故障数据组时出错: {e}")
            # 出错时，将整个文件作为一个故障组
            if len(df) > 0:
                fault_groups = [df]
        
        log_print(f"{filename}: 提取到 {len(fault_groups)} 个故障数据组")
        
        # 对每组数据进行处理
        for group in fault_groups:
            try:
                # 存储未处理的特征（只存储关键列）
                key_columns = ['AX', 'AY', 'AZ', 'GX', 'GY', 'GZ']
                available_columns = [col for col in key_columns if col in group.columns]
                unprocessed_features = group[available_columns].copy() if available_columns else group.copy()
                
                # 对数据进行归一化
                normalized_data = feature_extractor.normalize(group)
                
                # 提取特征
                processed_features = feature_extractor.extract_features(normalized_data)
                
                # 创建数据字典
                data_dict = {
                    'filename': filename,
                    'unprocessed_features': unprocessed_features.to_dict('records'),
                    'processed_features': processed_features,
                    'label': 'abnormal'
                }
                dataset.append(data_dict)
            except Exception as e:
                log_print(f"{filename}: 处理数据组时出错: {e}")
                continue
    
    # 保存数据集
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    
    log_print(f"故障数据处理完成，共处理 {len(dataset)} 条数据")
    return dataset

def process_normal_data(normal_dir, output_file, window_size=25):
    """处理正常数据"""
    log_print("开始处理正常数据...")
    
    # 创建数据处理器
    normal_processor = DataProcessor()
    normal_processor.add_file(normal_dir)
    
    # 调用filter处理数据
    normal_processor.data_process_with_filter_without_minus_means()
    
    # 创建特征提取器
    feature_extractor = FeatureExtractor()
    
    # 存储处理后的数据集
    dataset = []
    
    # 处理每个文件
    for item in normal_processor.file_contents:
        filename = item['filename']
        log_print(f"处理正常文件: {filename}")
        
        df = item['content'].copy()
        
        # 使用滑动窗口提取数据组
        normal_groups = []
        step_size = window_size  # 滑动步长等于窗口大小
        
        try:
            for i in range(0, len(df) - window_size + 1, step_size):
                window_data = df.iloc[i:i+window_size]
                normal_groups.append(window_data)
            
            # 如果窗口大小太大，调整窗口大小
            if len(normal_groups) == 0 and len(df) > 0:
                adjusted_window_size = max(10, len(df) // 5)
                log_print(f"{filename}: 调整窗口大小为 {adjusted_window_size} 以适应数据长度")
                for i in range(0, len(df) - adjusted_window_size + 1, adjusted_window_size):
                    window_data = df.iloc[i:i+adjusted_window_size]
                    normal_groups.append(window_data)
                
                # 如果仍然没有数据组，将整个文件作为一个组
                if len(normal_groups) == 0:
                    normal_groups = [df]
                    log_print(f"{filename}: 使用整个文件作为一个正常数据组")
        except Exception as e:
            log_print(f"{filename}: 提取正常数据组时出错: {e}")
            # 出错时，将整个文件作为一个数据组
            if len(df) > 0:
                normal_groups = [df]
        
        log_print(f"{filename}: 提取到 {len(normal_groups)} 个正常数据组")
        
        # 对每组数据进行处理
        for group in normal_groups:
            try:
                # 存储未处理的特征（只存储关键列）
                key_columns = ['AX', 'AY', 'AZ', 'GX', 'GY', 'GZ']
                available_columns = [col for col in key_columns if col in group.columns]
                unprocessed_features = group[available_columns].copy() if available_columns else group.copy()
                
                # 对数据进行归一化
                normalized_data = feature_extractor.normalize(group)
                
                # 提取特征
                processed_features = feature_extractor.extract_features(normalized_data)
                
                # 创建数据字典
                data_dict = {
                    'filename': filename,
                    'unprocessed_features': unprocessed_features.to_dict('records'),
                    'processed_features': processed_features,
                    'label': 'normal'
                }
                dataset.append(data_dict)
            except Exception as e:
                log_print(f"{filename}: 处理数据组时出错: {e}")
                continue
    
    # 保存数据集
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    
    log_print(f"正常数据处理完成，共处理 {len(dataset)} 条数据")
    return dataset

def create_final_dataset(abnormal_dataset, normal_dataset, output_file):
    """合并故障和正常数据集"""
    log_print("合并故障和正常数据集...")
    
    # 合并数据集
    final_dataset = abnormal_dataset + normal_dataset
    
    # 打乱数据集
    np.random.shuffle(final_dataset)
    
    # 保存最终数据集
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(final_dataset, f, ensure_ascii=False, indent=2)
    
    log_print(f"最终数据集创建完成，共 {len(final_dataset)} 条数据")
    log_print(f"故障数据: {len(abnormal_dataset)} 条")
    log_print(f"正常数据: {len(normal_dataset)} 条")


def part1_feature_extraction(data_for_feature_extraction):
    """
    对电梯故障数据集进行特征提取。
    """
    cols = ['AX','AY','AZ','GX','GY','GZ']
    feature_names = ["magnitude_means","magnitude_std","magnitude_rms","magnitude_peak","magnitude_skew","magnitude_kurtosis","magnitude_form_factor","magnitude_impulse_factor","magnitude_clearance_factor","magnitude_abs_means","spectral_peak","spectral_centroid","mean_square_frequency","frequency_variance","spectral_entropy","sample_entropy","permutation_entropy"]
    cols_feaure_names = []
    for col in cols:
        for feature_name in feature_names:
            cols_feaure_names.append(col+"_"+feature_name)


    warnings.filterwarnings("ignore", category=DeprecationWarning)
    # 设置数据集路径
    abnormal_dir = data_for_feature_extraction['abnormal_dir']
    normal_dir = data_for_feature_extraction['normal_dir']
    output_file = data_for_feature_extraction['output_file']
    csv_output_file = data_for_feature_extraction['csv_output_file'] 

    # abnormal_dir = r'故障数据集'
    # normal_dir = r'正常数据集'
    # output_file = 'elevator_fault_dataset.json'
    # csv_output_file = 'elevator_fault_dataset.csv' 



    log_print("开始创建电梯故障检测数据集...")

    # 处理故障数据
    abnormal_dataset = process_abnormal_data(abnormal_dir, 'abnormal_temp.json')

    # 处理正常数据
    normal_dataset = process_normal_data(normal_dir, 'normal_temp.json', window_size=25)


    # 合并数据集
    combined_dataset = abnormal_dataset + normal_dataset
    log_print(f"数据集合并完成，总数据量: {len(combined_dataset)}")

    # 打乱数据集顺序
    import random
    random.shuffle(combined_dataset)

    # 保存合并后的数据集
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(combined_dataset, f, ensure_ascii=False, indent=2)

    log_print(f"最终数据集已保存至: {output_file}")
    log_print(f"故障数据数量: {len(abnormal_dataset)}")
    log_print(f"正常数据数量: {len(normal_dataset)}")

    # 遍历combined_dataset，将'processed_features'与'label'一一配对
    import csv
    try:
        with open(csv_output_file, 'w', encoding='utf-8', newline='') as csvfile:
            writer = None
            for item in combined_dataset:
                processed_features = item['processed_features']
                label = item['label']
                row = processed_features + [label]
                if writer is None:
                    # 写入表头
                    header = [f'{cols_feaure_names[i]}' for i in range(len(processed_features))] + ['label']
                    writer = csv.writer(csvfile)
                    writer.writerow(header)
                writer.writerow(row)
        log_print(f"CSV格式数据集已保存至: {csv_output_file}")
    except Exception as e:
        log_print(f"保存CSV文件时出错: {e}")

    log_print("数据集创建完成!")

    # 清理临时文件
    try:
        if os.path.exists('abnormal_temp.json'):
            os.remove('abnormal_temp.json')
        if os.path.exists('normal_temp.json'):
            os.remove('normal_temp.json')
    except Exception as e:
        log_print(f"清理临时文件时出错: {e}")


import pandas as pd
from sklearn.decomposition import PCA
import numpy as np
from sklearn.impute import SimpleImputer
import os
import matplotlib.pyplot as plt

def part2_pca(data_for_pca):
    """
    对电梯故障数据集进行主成分分析（PCA），并将结果保存到CSV文件中。
    """
    # 读取数据
    try:
        data = pd.read_csv(data_for_pca)
    except FileNotFoundError:
        print(f"未找到 {data_for_pca} 文件，请检查文件路径。")
        return

    # 检查数据中是否包含 'label' 列
    if 'label' in data.columns:
        # 分离标签列
        labels = data['label']
        features = data.drop(columns=['label'])
    else:
        print(f"未找到 {data_for_pca} 中的 'label' 列，将使用全部数据进行分析。")
        features = data

    # 使用均值填充缺失值
    imputer = SimpleImputer(strategy='mean')
    features_imputed = imputer.fit_transform(features)

    # 初始化PCA，设置保留90%的方差
    pca = PCA(n_components=0.90, svd_solver='auto')
    
    # 执行主成分分析
    reduced_data = pca.fit_transform(features_imputed)
    
    # 打印保留的特征数量
    print(f"保留的特征数量: {reduced_data.shape[1]}")
    
    # 将降维后的数据转换为DataFrame
    reduced_df = pd.DataFrame(reduced_data, columns=[f'PC{i+1}' for i in range(reduced_data.shape[1])])
    
    # 输出每个主成分中贡献最大的原始特征
    components = pd.DataFrame(pca.components_, columns=features.columns, index=[f'PC{i+1}' for i in range(reduced_data.shape[1])])
    
    # 将每个新特征中各原始特征的贡献情况保存到csv文件
    components.to_csv('pca_components_contribution.csv', index=True)
    print("\n每个新特征中各原始特征的贡献情况已保存到 'pca_components_contribution.csv'")
    
    # 创建“特征贡献图”文件夹
    if not os.path.exists('PCA特征贡献图'):
        os.makedirs('PCA特征贡献图')
    
    # 对于每个新特征贡献前五的特征绘制直方图可视化表现
    for pc in components.index:
        top_features = components.loc[pc].abs().sort_values(ascending=False).head(5)  # 取每个主成分中绝对值最大的前5个特征
        plt.figure(figsize=(10, 6))
        ax = top_features.plot(kind='bar')
        plt.title(f'{pc} 中贡献较大的前5个特征')
        plt.xlabel('原始特征')
        plt.ylabel('贡献绝对值')
        plt.xticks(rotation=45)
        # 在每个柱子上添加对应的贡献值
        for p in ax.patches:
            ax.annotate(format(p.get_height(), '.4f'),
                        (p.get_x() + p.get_width() / 2., p.get_height()),
                        ha='center', va='center',
                        xytext=(0, 9),
                        textcoords='offset points')
        plt.tight_layout()
        plt.savefig(os.path.join('PCA特征贡献图', f'{pc}_top5_features.png'))
        plt.close()
    
    print("\n每个新特征贡献前五的特征直方图已保存到 'PCA特征贡献图' 文件夹")
    
    # 如果存在标签列，将标签列添加回降维后的数据
    if 'label' in data.columns:
        reduced_df['label'] = labels
    
    # 保存新数据集
    reduced_df.to_csv('elevator_fault_dataset_reduced.csv', index=False)
    print("\n保留重要特征后的新数据集已保存为 'elevator_fault_dataset_reduced.csv'")


import pandas as pd
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from imblearn.over_sampling import SMOTE
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
# 读取数据

def part3_train(data_for_train):
    """
    对电梯故障数据集进行训练和评估，使用集成学习方法（XGBoost）和随机森林，并通过网格搜索调参。
    最后保存两个最好效果的随机森林和XGBoost模型。
    """
    # 读取数据
    try:
        data = pd.read_csv(data_for_train)
    except FileNotFoundError:
        print(f"未找到 {data_for_train} 文件，请检查文件路径。")
        return

    # 假设最后一列为目标变量，其余为特征
    X = data.iloc[:, :-1]
    y = data.iloc[:, -1]

    print(y[:10])

    # 使用LabelEncoder对目标变量进行编码
    from sklearn.preprocessing import LabelEncoder
    le = LabelEncoder()
    y = le.fit_transform(y)

    # print(y[:10])

    # 输出正例和反例的数量及比例
    print("\n正例和反例的比例:")
    print(sum(y) / len(y))

    # 添加数据归一化步骤
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # 划分训练集和测试集
    from sklearn.model_selection import train_test_split
    X_train, X_test, y_train, y_test = train_test_split(X_scaled, y, test_size=0.3, random_state=42)

    X_train_res, y_train_res = X_train, y_train

    # 导入必要的库
    from xgboost import XGBClassifier
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import GridSearchCV
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
    import joblib

    # 定义模型和对应的参数网格
    models = {
        "XGBoost": {
            "model": XGBClassifier(scale_pos_weight=(len(y_train_res) - sum(y_train_res)) / sum(y_train_res)),
            "params": {
                'n_estimators': [50, 100, 200],
                'learning_rate': [0.01, 0.1, 0.2],
                'max_depth': [3, 5, 7]
            }
        },
        "Random Forest": {
            "model": RandomForestClassifier(class_weight='balanced', random_state=42),
            "params": {
                'n_estimators': [50, 100, 200],
                'max_depth': [None, 10, 20, 30],
                'min_samples_split': [2, 5, 10]
            }
        }
    }

    # 使用网格搜索调参并评估模型
    for name, model_info in models.items():
        print(f"\n正在对 {name} 模型进行网格搜索调参...")
        grid_search = GridSearchCV(model_info["model"], model_info["params"], cv=5, scoring='accuracy', n_jobs=-1)
        grid_search.fit(X_train_res, y_train_res)
        
        print(f"{name} 模型最佳参数: {grid_search.best_params_}")
        best_model = grid_search.best_estimator_
        
        # 预测测试集
        y_pred = best_model.predict(X_test)
        
        # 评估模型
        accuracy = accuracy_score(y_test, y_pred)
        print(f"{name} 模型准确率: {accuracy:.2f}")
        print("\n分类报告:")
        print(classification_report(y_test, y_pred))
        print("\n混淆矩阵:")
        print(confusion_matrix(y_test, y_pred))

        # 保存模型
        model_filename = f'{name.replace(" ", "_")}_best_model.pkl'
        try:
            joblib.dump(best_model, model_filename)
            print(f"{name} 最佳模型已保存至 {model_filename}")
        except Exception as e:
            print(f"保存 {name} 模型时出错: {e}")

if __name__ == '__main__':
    data_for_feature_extraction = {
        'abnormal_dir': '故障数据集-test',
        'normal_dir': '正常数据集-test',
        'output_file': 'elevator_fault_dataset_test.json',
        'csv_output_file': 'elevator_fault_dataset.csv'
    }
    data_for_pca = data_for_feature_extraction['csv_output_file']
    data_for_train = 'elevator_fault_dataset_reduced.csv'


    # part1_feature_extraction(data_for_feature_extraction)
    # part2_pca(data_for_pca)
    part3_train(data_for_train)