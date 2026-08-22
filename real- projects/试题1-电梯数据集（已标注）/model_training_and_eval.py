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
file_path = 'elevator_fault_dataset_reduced.csv'
data = pd.read_csv(file_path)

# 假设最后一列为目标变量，其余为特征
X = data.iloc[:, :-1]
y = data.iloc[:, -1]

print(y[:10])

# 使用LabelEncoder对目标变量进行编码
le = LabelEncoder()
y = le.fit_transform(y)

print(y[:10])

# 输出正例和反例的数量及比例
print("\n正例和反例的比例:")
print(sum(y) / len(y))

# 划分训练集和测试集
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

# 使用SMOTE解决数据不平衡问题
smote = SMOTE(random_state=42)
X_train_res, y_train_res = smote.fit_resample(X_train, y_train)

# 定义多个机器学习模型
models = {
    "XGBoost": XGBClassifier(scale_pos_weight=(len(y_train_res) - sum(y_train_res)) / sum(y_train_res)),
    "Random Forest": RandomForestClassifier(class_weight='balanced', random_state=42),
    "Logistic Regression": LogisticRegression(class_weight='balanced', max_iter=1000),
    "SVM": SVC(class_weight='balanced'),
    "Gaussian Naive Bayes": GaussianNB(),
    "KNN": KNeighborsClassifier()
}

# 训练并评估各个模型
for name, model in models.items():
    print(f"\n正在训练 {name} 模型...")
    model.fit(X_train_res, y_train_res)
    
    # 预测测试集
    y_pred = model.predict(X_test)
    
    # 评估模型
    accuracy = accuracy_score(y_test, y_pred)
    print(f"{name} 模型准确率: {accuracy:.2f}")
    print("\n分类报告:")
    print(classification_report(y_test, y_pred))
    print("\n混淆矩阵:")
    print(confusion_matrix(y_test, y_pred))
