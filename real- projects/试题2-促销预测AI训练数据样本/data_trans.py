import pandas as pd

table_info = {
    'Sales Transaction': ['KEY_VBELN', 'KEY_POSNR', 'RU_DATE', 'RU_TIME', 'RU_TCODE', 'RU_UNAME', 'LAST_MOD_DT', 'LIFNR', 'WERKS', 'VBELN', 'POSNR', 'MATNR', 'CHARG', 'FKIMG', 'VRKME', 'BRTWR', 'BRTWRPER', 'RTLPC', 'RTLPC_C', 'WAVWR', 'DISCNT', 'DISCNTPER', 'MWSBP', 'ERNAM', 'ERDAT', 'FKDAT', 'SPTAG', 'BONNR', 'CONDNR', 'PRVBT', 'XBLNR', 'KVORG', 'ZBKCH', 'ZAFCH', 'ZBFCH', 'VKFNR', 'KBDNR', 'VKORG', 'ALAND', 'ZSTONE', 'ZCATEG', 'ZSHAPE', 'ZMATER', 'ZMETAL', 'ZCOLORG', 'ZMSSUB', 'ZFASHI', 'ZSIZE', 'ZEWJMODEL', 'ZEWJVER', 'ZCERTI', 'ZOTHERS', 'ZBRAND', 'ZCARAT', 'ZMSMES', 'ZCLARI', 'ZCUTSTY', 'ZCUTGRADE', 'ZPOLISH', 'ZSYMME', 'ZFLORES', 'ZTDWEIG', 'ZTGWEIG', 'ZTPWEIG', 'ZSTOCK', 'ZLBSAL', 'ZGDCAT', 'ZWEIGH', 'ZLABOUR', 'ZMODELNO', 'ZSERIES', 'ZGENDER', 'ZMOVENT', 'ZDDIAL', 'ZDBEZEL', 'ZDBRACE', 'ZBRACE', 'ZDCOLOR', 'ZDPATTE', 'ZDNUMER', 'ZSCOLOR', 'ZSTGRP', 'ZTBRACE', 'ZCOLOR', 'ZSCATE', 'ZDIAMOND', 'ZCOLLECTION', 'ZDETAILCAT', 'ZGR_DATE', 'ZCASE_NO', 'ZCERT_NO', 'MTART', 'MATKL', 'MTBEZ', 'BRTWR_A', 'BRTWRPER_A', 'VIPNA', 'VIPNR', 'ZNS04', 'TRANS_TIME', 'REGIO', 'ZCCP', 'ZZSD', 'TRANS_IRS', 'POSINVNR_R', 'POSINVDA_R', 'ZEVENTID', 'ZPROCODE', 'POSGRPNO', 'SALESM2', 'SALESM3', 'SALESM4', 'SALESM5', 'RFPPL', 'RFSHOP', 'ESA', 'ZSPCOST', 'ZSPBFGP', 'ZSPAFGP', 'SACH2', 'ECTRX', 'ECTENDER', 'ZPROFLAG', 'ZAFCH_XP', 'ZBFCH_XP', 'BRTWR_XP', 'BRTWR_A_XP', 'REFST', 'ZLGORT', 'ZLOC_DAT'],
    'Site Master': ['WERKS', 'NAME1', 'BWKEY', 'NAME2', 'ORT01', 'EKORG', 'VKORG', 'LAND1', 'VLFKZ'],
    'Article master (Standard)': ['MATNR', 'ERSDA', 'MTART', 'MEINS'],
    'Article master (Custom)': ['MATNR', 'ZSTONE', 'ZCATEG', 'ZSHAPE', 'ZMATER', 'ZMETAL', 'ZCOLORG', 'ZMSSUB', 'ZFASHI', 'ZSIZE', 'ZSIZE_UOM', 'ZEWJMODEL', 'ZEWJVER', 'ZCERTI', 'ZOTHERS', 'ZBRAND', 'ZCARAT', 'ZCARAT_FR', 'ZCARAT_TO', 'ZMSMES', 'ZCLARI', 'ZCUTSTY', 'ZCUTGRADE', 'ZPOLISH', 'ZSYMME', 'ZFLORES', 'ZTDWEIG', 'ZTDWEIG_UOM', 'ZTGWEIG', 'ZTGWEIG_UOM', 'ZTPWEIG', 'ZTPWEIG_UOM', 'ZSTOCK', 'ZLBSAL', 'ZGDCAT', 'ZWEIGH', 'ZWEIGH_UOM', 'ZLABOUR', 'ZMODELNO', 'ZSERIES', 'ZGENDER', 'ZMOVENT', 'ZDDIAL', 'ZDBEZEL', 'ZDBRACE', 'ZBRACE', 'ZDCOLOR', 'ZDPATTE', 'ZDNUMER', 'ZSCOLOR', 'ZSTGRP', 'ZTBRACE', 'ZCOLOR', 'ZSCATE', 'ZDIAMOND', 'ZCOLLECTION', 'ZPDTSTATUS', 'ZDETAILCAT', 'ZPRODUCTCODE', 'ZEXPPERIOD', 'ZEXPDATE', 'ZMARKUP', 'ZKEYWORD', 'ZCRAFTS', 'ZCOMPL', 'ZFUNCT', 'ZSUB_COLLECTION', 'ZORIGIN', 'ZCOMMENT', 'ZEXTENSION', 'ZCERTI_TYPE', 'ZBRANDSUB', 'ZSIZETEXT', 'ZSDISCG', 'ZECOM_MODELNO', 'ZCOUPTY', 'ZREF_CODE', 'ZBUSTY', 'ZOTHER_COST', 'ZCCOBUO', 'ZDL', 'ZXL', 'JV_FLAG', 'ZORI_CODE'],
    'Article Description': ['MATNR', 'SPRAS', 'VKORG', 'ZDESC'],
    'Main Stone (Text)': ['Language', 'Article Type', 'Main Stone Code', 'Main Stone Description'],
    'Category (Text)': ['Language', 'Article Type', 'Category Code', 'Category Description'],
    'Shape (Text)': ['Language', 'Article Type', 'Shape Code', 'Shape Description'],
    'Material (Text)': ['Language', 'Article Type', 'Material Code', 'Material Description'],
    'Metal (Text)': ['Language', 'Article Type', 'Metal Code', 'Metal Description'],
    'Color Grade (Text)': ['Language', 'Article Type', 'Color Grade Code', 'Color Grade Description'],
    'Main Stone Sub-category (Text)': ['Language', 'Article Type', 'Main Stone Sub-category Code', 'Main Stone Sub-category Description'],
    'Style Type (Text)': ['Language', 'Article Type', 'Style Type Code', 'Style Type Description'],
    'Brand (Text)': ['Language', 'Article Type', 'Brand Code', 'Brand Description'],
    'Clarity (Text)': ['Language', 'Article Type', 'Clarity Code', 'Clarity Description'],
    'Cutting Style (Text)': ['Language', 'Article Type', 'Cutting Style Code', 'Cutting Style Description'],
    'Cut Grade (Text)': ['Language', 'Article Type', 'Cut Grade Code', 'Cut Grade Description'],
    'Polish (Text)': ['Language', 'Article Type', 'Polish Code', 'Polish Description'],
    'Symmetry (Text)': ['Language', 'Article Type', 'Symmetry Code', 'Symmetry Description'],
    'Fluorescence (Text)': ['Language', 'Article Type', 'Fluorescence Code', 'Fluorescence Description'],
    'Stock Source (Text)': ['Language', 'Article Type', 'Stock Source Code', 'Stock Source Description'],
    'Type (Text)': ['Language', 'Article Type', 'Type Code', 'Type Description'],
    'Gender (Text)': ['Language', 'Article Type', 'Gender Code', 'Gender Description'],
    'Movement (Text)': ['Language', 'Article Type', 'Movement Code', 'Movement Description'],
    'Diamond Dial (Text)': ['Language', 'Article Type', 'Diamond Dial Code', 'Diamond Dial Description'],
    'Diamond Bezel (Text)': ['Language', 'Article Type', 'Diamond Bezel Code', 'Diamond Bezel Description'],
    'Diamond Bracelet (Text)': ['Language', 'Article Type', 'Diamond Bracelet Code', 'Diamond Bracelet Description'],
    'Bracelet (Text)': ['Language', 'Article Type', 'Bracelet Code', 'Bracelet Description'],
    'Dial Color (Text)': ['Language', 'Article Type', 'Dial Color Code', 'Dial Color Description'],
    'Dial Pattern (Text)': ['Language', 'Article Type', 'Dial Pattern Code', 'Dial Pattern Description'],
    'Dial Numerals (Text)': ['Language', 'Article Type', 'Dial Numerals Code', 'Dial Numerals Description'],
    'Strap Color (Text)': ['Language', 'Article Type', 'Strap Color Code', 'Strap Color Description'],
    'Stock Turnover Group (Text)': ['Language', 'Article Type', 'Stock Turnover Group Code', 'Stock Turnover Group Description'],
    'Type of Bracelet (Text)': ['Language', 'Article Type', 'Type of Bracelet Code', 'Type of Bracelet Description'],
    'Color (Text)': ['Language', 'Article Type', 'Color Code', 'Color Description'],
    'Sub Category (Text)': ['Language', 'Article Type', 'Sub Category Code', 'Sub Category Description'],
    'Diamond (Text)': ['Language', 'Article Type', 'Diamond Code', 'Diamond Description'],
    'Certificate (Text)': ['Language', 'Article Type', 'Certificate Code', 'Certificate Description'],
    'Collection (Text)': ['Language', 'Article Type', 'Collection Code', 'Collection Description'],
    'Product Status (Text)': ['Language', 'Article Type', 'Product Status Code', 'Product Status Description'],
    'Segment (Text)': ['Language', 'Article Type', 'Segment Code', 'Segment Description'],
    'Mark Up Rate (Text)': ['Language', 'Article Type', 'Mark Up Rate Code', 'Mark Up Rate Description'],
    'Craftsmanship (Text)': ['Language', 'Article Type', 'Craftsmanship Code', 'Craftsmanship Description'],
    'Complication (Text)': ['Language', 'Article Type', 'Complication Code', 'Complication Description'],
    'Function (Text)': ['Language', 'Article Type', 'Function Code', 'Function Description'],
    'Fixed Price Type (Text)': ['Language', 'Article Type', 'Fixed Price Type Code', 'Fixed Price Type Description'],
    'PRPO Reason Category (Text)': ['Language', 'Article Type', 'Reason Category Code', 'Reason Category Description'],
    'Sub Collection (Text)': ['Language', 'Article Type', 'Sub Collection Code', 'Sub Collection Description'],
    'Origin (Text)': ['Language', 'Article Type', 'Origin Code', 'Origin Description'],
    'Comment (Text)': ['Language', 'Article Type', 'Comment Code', 'Comment Description'],
    'Certificate Type (Text)': ['Language', 'Article Type', 'Certificate Type Code', 'Certificate Type Description'],
    'Sub Product Line (Text)': ['Language', 'Article Type', 'Sub Product Line Code', 'Sub Product Line Description'],
    'Sales Discount Group(Text)': ['Language', 'Article Type', 'Sales Discount Group Code', 'Sales Discount Group Description'],
    'Coupon Type (Text)': ['Language', 'Article Type', 'Coupon Type Code', 'Coupon Type Description'],
    'Business Type (Text)': ['Language', 'Article Type', 'Business Type  Code', 'Business Type Description'],
    'Monthly Staff List': ['STAFFCODE', 'YEAR_MONTH', 'WERKS', 'ZCOMM_BUSTYPE', 'STAFFNAME', 'STAFFALIAS', 'CHINESENAME', 'TITLE', 'ZEMP_TYPE', 'GRADING', 'DIVISION', 'ZBRAND', 'DATEJOIN', 'SHOPPOINT', 'GROUPPOINT', 'TEMPGROUPPOINT', 'TMP_GP_EFFDATE', 'PL_DAYS', 'NPL_DAYS', 'ZLAST_MOD_DT', 'RU_TCODE', 'RU_UNAME', 'RU_DATE', 'ZDEL'],
    'Position Rate': ['WERKS', 'ZCOMM_BUSTYPE', 'TITLE', 'ZVALID_TO_YM', 'ZVALID_FR_YM', 'ZPOS_RATE', 'ZAWARD_FLG', 'ZLAST_MOD_DT', 'RU_TCODE', 'RU_UNAME', 'RU_DATE', 'ZDEL'],
    'Sales Promotion Item': ['IDOCNR', 'ITEM', 'PRO_CODE', 'PRO_DESC'],
    'Sales Header': ['IDOCNR', 'IDOCTYP', 'POSINVNR', 'IDOCDAT', 'IDOCTIM', 'IDOCSTA', 'WERKS', 'POSINTYP', 'POSDCURR', 'POSKREIS', 'KASSID', 'KASSIERER', 'POSREDOC', 'POSMNINV', 'POSINVDA', 'POSVIPNR', 'POSVIPNA', 'POSBILNR', 'POSARTNR', 'POSHAGOLD', 'POSZCGST', 'POSGRPNO', 'SALESM2', 'SALESM3', 'SALESM4', 'SALESM5', 'RFPPL', 'RFSHOP', 'ESA', 'SACH2', 'ECTRX', 'REFST'],
    'Sales Item Details': ['IDOCNR', 'ITEM', 'KSCHL', 'VORGANGART', 'MATNR', 'CHARG', 'MENGE', 'MENGE2', 'VERKAEUFER', 'APPVID', 'WAVWR', 'ZLGORT', 'ZLOC_DAT'],
    'Promotion Header': ['pmt_no', 'pmt_date', 'pmt_start', 'pmt_end', 'pmt_status', 'pmt_remark', 'pmt_level', 'type_id', 'updated_on', 'ho_upd_on']
}

en2ch_header = {

    "通用字段": {
      "KEY_": "复合主键前缀",
      "RU_": "系统操作记录前缀",
      "Z": "自定义字段前缀",
      "IDOC": "单据相关前缀"
    },
    
      "VBELN": "销售单据编号",
      "POSNR": "行项目号",
      "MATNR": "商品编号",
      "CHARG": "批次编号",
      "FKIMG": "销售数量",
      "VRKME": "销售单位",
      "BRTWR": "销售金额",
      "BRTWRPER": "金额百分比",
      "RTLPC": "零售价",
      "WAVWR": "成本价",
      "DISCNT": "折扣金额",
      "DISCNTPER": "折扣百分比",
      "FKDAT": "过账日期",
      "ERDAT": "创建日期",
      "SPTAG": "发货日期"
    ,

      "ZBRAND": "品牌",
      "ZCATEG": "品类",
      "ZMETAL": "金属材质",
      "ZGENDER": "性别",
      "ZSERIES": "系列",
      "ZMODELNO": "型号",
      "ZCARAT": "克拉重量",
      "ZCLARI": "净度等级",
      "ZCUTGRADE": "切工等级",
      "ZCOLORG": "颜色等级",
      "ZMOVENT": "机芯类型",
      "ZSTONE": "主石类型",
      "ZSHAPE": "形状",
      "ZMATER": "材质",
      "ZMSSUB": "主石子类",
      "ZFASHI": "时尚类型",
      "ZSIZE": "尺寸",
      "ZCERTI": "证书类型",
      "ZOTHERS": "其他属性"
    ,
    

      "WERKS": "门店/工厂代码",
      "VKORG": "销售组织",
      "ALAND": "国家代码",
      "REGIO": "区域",
      "LIFNR": "供应商编号"
    ,
    

      "VIPNR": "VIP客户编号",
      "VIPNA": "VIP客户姓名",
      "KBDNR": "客户编号"
    ,
    

      "ZPROCODE": "促销代码",
      "BONNR": "促销编号",
      "pmt_no": "促销编号",
      "pmt_start": "促销开始日期",
      "pmt_end": "促销结束日期",
      "pmt_status": "促销状态",
      "pmt_remark": "促销描述"
    ,
    

      "STAFFCODE": "员工编号",
      "ZCOMM_BUSTYPE": "业务类型",
      "ZEMP_TYPE": "员工类型",
      "ZPOS_RATE": "职位费率",
      "VERKAEUFER": "销售员编号"
    ,
    

      "ZSPCOST": "标准成本",
      "ZSPBFGP": "前毛利润",
      "ZSPAFGP": "后毛利润",
      "ZSTOCK": "库存来源",
      "ZLBSAL": "标签销售"
    ,
    

      "TCODE": "事务代码",
      "UNAME": "用户名",
      "IDOCNR": "单据编号",
      "IDOCDAT": "单据日期",
      "IDOCTYP": "单据类型"
    ,

      "MTBEZ": "物料类型描述",
      "ZDESC": "商品描述"
    ,
    
      "WAT": "手表业务",
      "JEW": "珠宝业务",
      "HK": "香港",
      "MO": "澳门",
      "EA": "每个",
      "NS01": "标准销售",
      "RS02": "退货处理"
    ,

      "MTART": "物料类型",
      "MATKL": "物料组",
      "WATC": "手表",
      "WWTU": "手表类型"
    ,

      "ZCARAT": "克拉",
      "ZCLARI": "净度",
      "ZCUTGRADE": "切工",
      "ZCOLORG": "颜色",
      "ZFLORES": "荧光"
    ,
    
      "ZMOVENT": "机芯",
      "ZDDIAL": "表盘钻石",
      "ZDBEZEL": "表圈钻石",
      "ZDBRACE": "表带钻石",
      "ZGENDER": "适用性别"
    
  ,
  

    "核心关联字段": {
      "MATNR": "连接销售交易、商品主数据、商品描述",
      "WERKS": "连接销售交易、门店主数据、员工数据",
      "VBELN/POSINVNR/IDOCNR": "连接销售头表、交易明细、促销项目",
      "pmt_no/PRO_CODE": "连接促销头表与促销商品关联"
    },
    
    "业务流关系": {
      "Promotion Header → Sales Promotion Item → Sales Transaction": "促销活动到具体销售",
      "Site Master + Monthly Staff List → Sales Transaction": "门店员工绩效分析",
      "Article Master → Sales Transaction": "商品属性与销售关联",
      "Sales Header → Sales Item Details → Sales Transaction": "销售单据完整性"
    }
  ,
  
  "数据分析重点字段": {
    "促销效果分析": ["BRTWR", "DISCNTPER", "ZPROCODE", "FKDAT", "pmt_start", "pmt_end"],
    "客户行为分析": ["VIPNR", "BRTWR", "ZBRAND", "ZCATEG", "FKDAT"],
    "商品绩效分析": ["MATNR", "BRTWR", "ZBRAND", "ZCATEG", "FKIMG"],
    "库存关联分析": ["CHARG", "ZSTOCK", "ZSPCOST", "WAVWR"],
    "时序分析": ["FKDAT", "ERDAT", "pmt_start", "pmt_end"],
    "分布分析": ["BRTWR", "ZBRAND", "ZCATEG", "WERKS", "VIPNR"]
  }
}

en2ch_title = {
  "表名映射": {
    "Sales Transaction": "销售交易明细表",
    "Site Master": "门店主数据表",
    "Article master (Standard)": "商品标准主数据表",
    "Article master (Custom)": "商品自定义属性表",
    "Article Description": "商品描述表",
    "Main Stone (Text)": "主石文本表",
    "Category (Text)": "品类文本表",
    "Shape (Text)": "形状文本表",
    "Material (Text)": "材质文本表",
    "Metal (Text)": "金属文本表",
    "Color Grade (Text)": "颜色等级文本表",
    "Main Stone Sub-category (Text)": "主石子类文本表",
    "Style Type (Text)": "款式类型文本表",
    "Brand (Text)": "品牌文本表",
    "Clarity (Text)": "净度文本表",
    "Cutting Style (Text)": "切工样式文本表",
    "Cut Grade (Text)": "切工等级文本表",
    "Polish (Text)": "抛光文本表",
    "Symmetry (Text)": "对称性文本表",
    "Fluorescence (Text)": "荧光文本表",
    "Stock Source (Text)": "库存来源文本表",
    "Type (Text)": "类型文本表",
    "Gender (Text)": "性别文本表",
    "Movement (Text)": "机芯文本表",
    "Diamond Dial (Text)": "钻石表盘文本表",
    "Diamond Bezel (Text)": "钻石表圈文本表",
    "Diamond Bracelet (Text)": "钻石表带文本表",
    "Bracelet (Text)": "表带文本表",
    "Dial Color (Text)": "表盘颜色文本表",
    "Dial Pattern (Text)": "表盘图案文本表",
    "Dial Numerals (Text)": "表盘数字文本表",
    "Strap Color (Text)": "表带颜色文本表",
    "Stock Turnover Group (Text)": "库存周转组文本表",
    "Type of Bracelet (Text)": "表带类型文本表",
    "Color (Text)": "颜色文本表",
    "Sub Category (Text)": "子品类文本表",
    "Diamond (Text)": "钻石文本表",
    "Certificate (Text)": "证书文本表",
    "Collection (Text)": "系列文本表",
    "Product Status (Text)": "产品状态文本表",
    "Segment (Text)": "细分市场文本表",
    "Mark Up Rate (Text)": "加价率文本表",
    "Craftsmanship (Text)": "工艺文本表",
    "Complication (Text)": "复杂功能文本表",
    "Function (Text)": "功能文本表",
    "Fixed Price Type (Text)": "固定价格类型文本表",
    "PRPO Reason Category (Text)": "采购原因类别文本表",
    "Sub Collection (Text)": "子系列文本表",
    "Origin (Text)": "产地文本表",
    "Comment (Text)": "备注文本表",
    "Certificate Type (Text)": "证书类型文本表",
    "Sub Product Line (Text)": "子产品线文本表",
    "Sales Discount Group(Text)": "销售折扣组文本表",
    "Coupon Type (Text)": "优惠券类型文本表",
    "Business Type (Text)": "业务类型文本表",
    "Monthly Staff List": "月度员工清单表",
    "Position Rate": "职位费率表",
    "Sales Promotion Item": "促销商品关联表",
    "Sales Header": "销售单据头表",
    "Sales Item Details": "销售商品明细表",
    "Promotion Header": "促销活动头表"
  },
  
  "表分类说明": {
    "核心业务表": [
      "销售交易明细表",
      "销售单据头表", 
      "销售商品明细表",
      "促销活动头表",
      "促销商品关联表"
    ],
    "主数据表": [
      "门店主数据表",
      "商品标准主数据表",
      "商品自定义属性表",
      "商品描述表"
    ],
    "组织架构表": [
      "月度员工清单表",
      "职位费率表"
    ],
    "文本描述表": [
      "主石文本表",
      "品类文本表",
      "品牌文本表",
      "金属文本表",
      "净度文本表",
      "切工等级文本表",
      "机芯文本表"
    ]
  },
  
  "关键表关系": {
    "销售核心流": "促销活动头表 → 促销商品关联表 → 销售单据头表 → 销售商品明细表 → 销售交易明细表",
    "商品主数据流": "商品标准主数据表 → 商品自定义属性表 → 商品描述表 → 各种文本描述表",
    "组织数据流": "门店主数据表 → 月度员工清单表 → 职位费率表",
    "分析关联路径": "通过MATNR关联商品数据，通过WERKS关联门店数据，通过VBELN/IDOCNR关联销售单据"
  }
}
# 定义数据源
data_sources = [
    '销售数据',
    '库存水平',
    '促销详情（每个公众假期均有促销活动）',
    '外部数据-假期（data.gov.hk）',
    '外部数据-付费API （https://goldapi.io/）金价'
]

def read_excel_data():
    """
    从 “试题2-促销预测AI训练数据样本.xlsx” 文件中读取所有子表数据
    
    Returns:
        dict: 键为子表名，值为对应的DataFrame
    """
    file_path = "试题2-促销预测AI训练数据样本.xlsx"
    xls = pd.ExcelFile(file_path)
    data_frames = {}
    # 不再使用table_info解析
    for sheet_name in xls.sheet_names:
        data_frames[sheet_name] = xls.parse(sheet_name)
    return data_frames

def translate_column_names(df, en2ch_header):
    """
    将DataFrame的列名从英文翻译成中文
    
    Args:
        df (pd.DataFrame): 待翻译列名的DataFrame
        en2ch_header (dict): 英文到中文的列名映射字典
    
    Returns:
        pd.DataFrame: 列名已翻译的DataFrame
    """
    # 提取普通字段映射
    header_mapping = {k: v for k, v in en2ch_header.items() if not isinstance(v, dict)}
    translated_columns = {}
    for col in df.columns:
        if col in header_mapping:
            translated_columns[col] = header_mapping[col]
        else:
            # 检查通用字段前缀
            for prefix, prefix_ch in en2ch_header.get("通用字段", {}).items():
                if col.startswith(prefix):
                    # 去掉前缀后再匹配，如果没匹配到则保留原前缀翻译
                    rest_part = col[len(prefix):]
                    if rest_part in header_mapping:
                        translated_columns[col] = prefix_ch + header_mapping[rest_part]
                    else:
                        translated_columns[col] = prefix_ch + rest_part
                    break
            else:
                translated_columns[col] = col
    return df.rename(columns=translated_columns)

def data_filtering_and_cleaning(data_frames):
    """
    对读取的数据进行筛选和清洗
    
    Args:
        data_frames (dict): 键为子表名，值为对应的DataFrame
    
    Returns:
        dict: 清洗筛选后的数据，键为子表名，值为对应的DataFrame
    """
    cleaned_data = {}
    for sheet_name, df in data_frames.items():
        print(f"{sheet_name}")
        # 删除全为空值的列
        cleaned_df = df.dropna(axis=1, how='all')
        cleaned_data[sheet_name] = cleaned_df
    return cleaned_data

def translate_and_save_data(data_frames, en2ch_header, en2ch_title):
    """
    翻译表名和列名，并保存翻译和清洁后的新表
    
    Args:
        data_frames (dict): 键为子表名，值为对应的DataFrame
        en2ch_header (dict): 英文到中文的列名映射字典
        en2ch_title (dict): 英文到中文的表名映射字典
    """
    # 获取表名映射
    table_mapping = en2ch_title.get("表名映射", {})
    with pd.ExcelWriter('ch珠宝数据表.xlsx') as writer:
        for sheet_name, df in data_frames.items():
            # 翻译表名
            translated_sheet_name = table_mapping.get(sheet_name, sheet_name)
            # 翻译列名
            translated_df = translate_column_names(df, en2ch_header)
            # 保存数据
            translated_df.to_excel(writer, sheet_name=translated_sheet_name, index=False)

def extract_sales_features(data_frames):
    """
    提取典型销售特征和冗余特征
    
    Args:
        data_frames (dict): 键为子表名，值为对应的DataFrame
    
    Returns:
        tuple: (典型销售特征DataFrame, 冗余特征DataFrame)
    """
    # 这里需要根据业务需求定义典型销售特征和冗余特征
    # 简单示例：假设 Sales Transaction 表包含销售特征
    sales_df = data_frames.get('Sales Transaction', pd.DataFrame())
    # 假设典型销售特征包含部分列
    typical_features = sales_df[['KEY_VBELN', 'VBELN', 'POSNR', 'MATNR', 'FKIMG', 'BRTWR']]
    # 假设冗余特征为其他列
    redundant_features = sales_df.drop(columns=typical_features.columns)
    return typical_features, redundant_features

def statistical_analysis(data_frames):
    """
    按分布、时序、关联等方式进行统计分析
    
    Args:
        data_frames (dict): 键为子表名，值为对应的DataFrame
    """
    # 分布分析示例：查看 Sales Transaction 表中 FKIMG 列的分布
    sales_df = data_frames.get('Sales Transaction', pd.DataFrame())
    if not sales_df.empty:
        print("FKIMG 列的分布:")
        print(sales_df['FKIMG'].describe())
    
    # 时序分析示例：假设 RU_DATE 是日期列，进行按日期分组统计
    if 'RU_DATE' in sales_df.columns:
        sales_df['RU_DATE'] = pd.to_datetime(sales_df['RU_DATE'])
        daily_sales = sales_df.groupby(sales_df['RU_DATE'].dt.date)['BRTWR'].sum()
        print("\n每日销售总额:")
        print(daily_sales)
    
    # 关联分析示例：查看部分列的相关性
    correlation_cols = ['FKIMG', 'BRTWR', 'DISCNT']
    if all(col in sales_df.columns for col in correlation_cols):
        correlation = sales_df[correlation_cols].corr()
        print("\n相关系数矩阵:")
        print(correlation)

def data_processing():
    """
    数据处理函数，包含数据筛选、清洗和统计分析
    """
    # 读取数据
    data_frames = read_excel_data()
    
    # 数据筛选和清洗
    cleaned_data = data_filtering_and_cleaning(data_frames)
    
    # 提取典型销售特征和冗余特征
    # typical_features, redundant_features = extract_sales_features(cleaned_data)
    
    # 翻译并保存数据
    translate_and_save_data(cleaned_data, en2ch_header, en2ch_title)
    
    # 按分布、时序、关联等方式进行统计分析
    # statistical_analysis(cleaned_data)

# 仿照给定代码，读写ch珠宝数据表.xlsx，并打印表头和相应的字段
def print_excel_headers():
    """
    读取 'ch珠宝数据表.xlsx' 文件，打印每个子表的名称和表头
    """
    file_path = 'ch珠宝数据表.xlsx'
    # 读取 Excel 文件
    excel_file = pd.ExcelFile(file_path)

    # 获取所有表名
    sheet_names = excel_file.sheet_names

    # 用于存储子表名称和对应表头的信息
    output_info = []

    # 遍历每个子表
    for sheet_name in sheet_names:
        df = excel_file.parse(sheet_name)
        headers = df.columns.tolist()
        output_info.append(f"子表名称: {sheet_name}\n表头: {headers}\n")

    # 打印信息
    for info in output_info:
        print(info)

    # 将信息保存到 txt 文件
    with open('ch子表信息.txt', 'w', encoding='utf-8') as f:
        for info in output_info:
            f.write(info)

if __name__ == "__main__":
    data_processing()
    print_excel_headers()
