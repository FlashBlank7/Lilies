import {render, screen} from '@testing-library/react'
import {expect,it,vi} from 'vitest'
import {ProjectTaskOutput} from '@/app/components/ProjectRunPanel'
vi.mock('@/lib/platform',()=>({api:vi.fn(),withFrontendToken:(s:string)=>s}))
it('shows actual features, exclusions, missing values and downloadable stage artifacts',()=>{
 render(<ProjectTaskOutput projectId="p" task={{status:'succeeded',outputs:{features:{stage:'before_fold_preprocessing',source_rows:10,rows:9,columns:1,
  note:'缺失值填补在每个训练折内拟合',excluded:[{reason:'目标标签缺失',rows:1}],preview:[{__sample_index:2,temperature:null}],
  features:[{name:'temperature',source:'sensor',calculation:'mean'}],artifacts:[{file_path:'results/features/d/run/features.csv',label:'下载完整特征表 CSV'}]}}} as never}/> )
 expect(screen.getByRole('region',{name:'制备后的样本与特征'})).toHaveTextContent('保留 9 条')
 expect(screen.getByText('目标标签缺失：排除 1 条。')).toBeInTheDocument()
 expect(screen.getByRole('cell',{name:'缺失'})).toBeInTheDocument()
 expect(screen.getByRole('link',{name:'下载完整特征表 CSV ↓'})).toHaveAttribute('href','/api/platform/api/v1/applications/p/workspace/files/results/features/d/run/features.csv?download=1')
})
