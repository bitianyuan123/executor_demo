#!/usr/bin/env python3
"""Offline figures: reviewed aggregates only, never invented raw observations."""
from pathlib import Path
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon

HERE=Path(__file__).resolve().parent
FONT=Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
if not FONT.exists(): raise SystemExit('Install fonts-wqy-zenhei or supply this font path before rendering.')
font_manager.fontManager.addfont(str(FONT))
plt.rcParams.update({'font.family':font_manager.FontProperties(fname=str(FONT)).get_name(),
 'font.size':11,'axes.unicode_minus':False,'axes.spines.top':False,'axes.spines.right':False,
 'axes.labelcolor':'#25384a','text.color':'#25384a','axes.titleweight':'bold','svg.fonttype':'path',
 'savefig.facecolor':'white','figure.facecolor':'white'})
COL={'home':'#355C83','queue':'#C37C1E','active':'#8764AC','outstanding':'#188B83',
 'fork-parent':'#C37C1E','all-parent':'#188B83'}
D=json.loads((HERE/'data/comparisons.json').read_text())
RUNS=D['runs']; IDX={r['id']:r for r in RUNS}; PAIRS=D['paired_comparisons']
OUT=HERE/'figures'; OUT.mkdir(exist_ok=True)
MANIFEST=[]

def save(fig,name,title,sources,caption):
 for ext in ('png','svg'): fig.savefig(OUT/f'{name}.{ext}',dpi=170,bbox_inches='tight',pad_inches=.18)
 svg=OUT/f'{name}.svg'; svg.write_text('\n'.join(line.rstrip() for line in svg.read_text().splitlines())+'\n')
 MANIFEST.append({'name':name,'title':title,'sources':sources,'caption':caption})
 plt.close(fig)
def setup(title,subtitle='',size=(13,6)):
 fig,ax=plt.subplots(figsize=size)
 fig.suptitle(title,x=.06,y=.98,ha='left',fontsize=17)
 if subtitle:fig.text(.06,.918,subtitle,fontsize=10,color='#596a78')
 return fig,ax

def canvas(title,sub='',height=7):
 fig,ax=setup(title,sub,(14,height));ax.set_xlim(-3,103);ax.set_ylim(-3,101);ax.axis('off');fig.subplots_adjust(top=.86,bottom=.06)
 return fig,ax

def box(ax,x,y,w,h,label,color='#eaf0f6',fs=11,edge='#a7b6c4'):
 ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.45,rounding_size=1',facecolor=color,edgecolor=edge,lw=1.2))
 ax.text(x+w/2,y+h/2,label,ha='center',va='center',fontsize=fs,linespacing=1.5)
def arrow(ax,a,b,label='',color='#526b80',style='-',offset=(0,2)):
 ax.add_patch(FancyArrowPatch(a,b,arrowstyle='-|>',mutation_scale=14,color=color,lw=1.5,linestyle=style))
 if label:ax.text((a[0]+b[0])/2+offset[0],(a[1]+b[1])/2+offset[1],label,ha='center',va='bottom',fontsize=10,color=color,bbox={'facecolor':'white','edgecolor':'none','pad':1})
def foot(fig,text):fig.text(.06,.018,text,fontsize=9,color='#5b6976',va='bottom')
def pick(batch,**kwargs):return [r for r in RUNS if r['batch']==str(batch) and all(r.get(k)==v for k,v in kwargs.items())]
def pct(c,b,k):return 100*(c[k]/b[k]-1)
def grid(ax):ax.grid(axis='y',color='#e7ecf0');ax.set_axisbelow(True)

# 1. Scenario architecture. Diagram is explanatory, not measured timing.
fig,ax=canvas('图1  场景视图：一个业务总池，按实际 cluster 分组','蓝色为计算路径；绿色为可借资源；橙色为外部等待。六池只是本次资源选择。')
for x,lab in [(3,'客户端请求'),(28,'IO接收'),(53,'worker构图'),(78,'总池入口\n固定 request home')]: box(ax,x,82,19,12,lab,fs=11)
for x in (22,47,72):arrow(ax,(x,88),(x+6,88))
ax.text(36,76,'业务背景；IO与构图并未完整复刻',fontsize=10,ha='center',color='#687b8a')
box(ax,33,62,34,11,'图依赖满足 → 新 ready callback\nL2 按亲和与压力选池',color='#e3f3ef',fs=11)
arrow(ax,(87,82),(66,69));arrow(ax,(42,62),(25,55));arrow(ax,(58,62),(75,55))
for x,lab in [(3,'NUMA A（本次节点0）'),(53,'NUMA B（本次节点2）')]:
 box(ax,x,24,44,31,'',color='#f5f8fa');ax.text(x+22,51,lab,ha='center',fontsize=12,fontweight='bold')
 for j in range(3):box(ax,x+2+j*14,33,12,12,f'cluster {j+1}\nFolly子池',color='#e4efed' if j else '#e7edf5',fs=10)
 ax.text(x+22,27,'worker仅在本cluster允许CPU上运行',ha='center',fontsize=9)
 arrow(ax,(x+14,39),(x+16,39),color='#188b83');arrow(ax,(x+28,39),(x+30,39),color='#188b83')
arrow(ax,(47,39),(53,39),style='--');ax.text(50,44,'跨域\n默认关',ha='center',fontsize=9)
box(ax,34,3,32,12,'外部完成源模型（替代真实网络）\n等待结束后恢复保存的上下文',color='#fff0d8',fs=10)
arrow(ax,(25,24),(34,10),'发起等待',offset=(-3,0));arrow(ax,(66,10),(94,10));arrow(ax,(94,10),(94,67));arrow(ax,(94,67),(67,67),'恢复重新进入L2')
foot(fig,'图示架构已实现于研究工程；CPU绑定不等于绑定数据页；业务仍负责依赖、共享写安全和最终完成。')
save(fig,'01_scene','场景视图',['RequestExecutor.h','AsyncCompletionSource.h'],'示意图，不是生产部署实测拓扑。')

# 2. Sequence with load lifetimes.
fig,ax=canvas('图2  异步时序：等待释放 worker，恢复再次成为 CPU ready 工作','时序为因果示意，不按时间比例绘制；零延迟时 helper 可在注册者返回前派发。',height=8)
xs=[9,35,64,91];labs=['当前worker','总池 / Folly','外部完成源','目标worker']
for x,l in zip(xs,labs):
 box(ax,x-8,89,16,9,l,fs=11);ax.plot([x,x],[6,88],color='#b9c5ce',ls='--',lw=1)
arrow(ax,(9,82),(35,82),'保存request、parent及续接令牌')
arrow(ax,(9,73),(64,73),'业务算子完成；登记due')
box(ax,0,56,20,11,'原callback返回\n当前 I / O 退出',color='#e3f3ef',fs=10)
box(ax,45,53,37,10,'外部pending存在，worker已可做其他任务\n未就绪后继不计入CPU池负载',color='#fff0d8',fs=10)
arrow(ax,(64,46),(35,46),'到期后实际fire：提交rpc_resume',offset=(0,2))
ax.text(65,64,'issue→due\n配置等待',fontsize=9,ha='left');ax.text(66,43,'due→fire\n源延迟',fontsize=9,ha='left')
box(ax,24,29,25,11,'选池 → R预留 → O接纳\n入队 / permit通知',color='#e7edf5',fs=10)
arrow(ax,(35,28),(91,28),'worker获得CPU并领取闭包 → callback开始')
box(ax,81,13,19,10,'恢复callback执行\nI开始 → 推进依赖',color='#e3f3ef',fs=10)
arrow(ax,(91,9),(35,9),'提交下一业务算子，继承实际父池')
foot(fig,'submitted在选池/预留之后，非完整add成本。活跃worker与被唤醒worker竞争任务；等待期间request与续接lease仍存活。')
save(fig,'02_sequence','异步时序与计量边界',['main.cpp async path','RequestExecutor.h continuation'],'示意顺序；不保证zero-delay注册者先返回，不把不同阶段p99相加。')

# 3. Routing flow.
fig,ax=canvas('图3  新 ready callback 的放置流程','这是提交时选池：不搬运队列中的任务，不抢占运行算子；负载提供者和评分器可替换。',height=8)
box(ax,27,88,46,9,'固定home；偏好实际parent（无parent则home）',fs=11)
box(ax,23,71,54,13,'home/pin-home → 直接home；fork-only非fork / hop耗尽 → 直接偏好池\n其余：仅ReturnHome模式，home≤low且同域或允许跨域时可改偏好',fs=9)
arrow(ax,(23,77),(13,77));arrow(ax,(13,77),(13,51),'提前返回',offset=(-2,0))
arrow(ax,(50,88),(50,84))
box(ax,32,56,36,10,'偏好评分 > high ？',color='#fff0d8',fs=12);arrow(ax,(50,71),(50,66),'其余',offset=(5,0))
box(ax,2,40,22,11,'直接指定池\n或保留偏好池',fs=11);arrow(ax,(32,61),(13,51),'否')
box(ax,35,38,54,12,'是：同NUMA扫描\n严格更轻，且改善 > min-gain',color='#e3f3ef',fs=11);arrow(ax,(58,56),(58,50),'是',offset=(6,0))
box(ax,35,19,54,12,'无本域合格目标且允许跨NUMA？\n才搜索远端，并加同单位罚项',fs=11);arrow(ax,(62,38),(62,31))
box(ax,12,0,76,11,'选中目标 → chunk预留 → 逐个入队；无改善仍留偏好池\n“较轻”不保证idle；R/O是近似快照，并发提交可能选中同池',color='#e7edf5',fs=10)
arrow(ax,(13,40),(13,11));arrow(ax,(90,43),(96,43));arrow(ax,(96,43),(96,5));arrow(ax,(96,5),(89,5));ax.text(95,48,'本域有解',ha='right',fontsize=9)
arrow(ax,(62,19),(62,11))
save(fig,'03_routing','放置决策流程',['RequestExecutor.h choose/searchDomain'],'high/gain使用严格比较；ReturnHome独立于普通借出。')

# 4. Stage-3 conditional quantile ranges, not CI.
independent=json.loads((HERE/'data/independent_evidence.json').read_text())
independent_rows=independent['rows']
fig,axs=plt.subplots(1,2,figsize=(13,5.2));fig.suptitle('图4  Q能描述积压，但Q=0不能保证完成快',x=.06,ha='left',fontsize=17)
x=np.arange(len(independent_rows))
labels=[r['queue_bin'].replace('～','–')+'\n'+(str(r['tasks_per_run_range'][0]) if r['tasks_per_run_range'][0]==r['tasks_per_run_range'][1] else '–'.join(str(v) for v in r['tasks_per_run_range']))+'条/轮' for r in independent_rows]
lo=[[r[k][0] for r in independent_rows] for k in ['wait_p99_ms_range','submit_to_finish_p99_ms_range']]
hi=[[r[k][1] for r in independent_rows] for k in ['wait_p99_ms_range','submit_to_finish_p99_ms_range']]
for ax,a,b,title in zip(axs,lo,hi,['提交→开始 p99（ms）','提交→完成 p99（ms）']):
 for i,(l,h) in enumerate(zip(a,b)):
  ax.plot([i,i],[l,h],color=COL['home'],lw=5,solid_capstyle='round');ax.scatter([i,i],[l,h],color=COL['home'],s=22)
  ax.annotate(f'{l:.3f}–{h:.3f}' if i==0 and title.startswith('提交→开始') else f'{l:.1f}–{h:.1f}',(i,h),xytext=(0,10),textcoords='offset points',ha='center',fontsize=9)
 ax.set_xticks(x,labels);ax.set_xlabel('提交前Q分桶');ax.set_title(title,fontsize=12);grid(ax);ax.margins(x=.15,y=.22)
 ax.axvspan(-.4,.4,color='#fff0d8',zorder=0)
fig.subplots_adjust(bottom=.24,top=.82,wspace=.25)
foot(fig,'独立任务：cluster / 每池256线程 / local burst100；线段为3个seed各自p99的范围，不是置信区间。Q=0桶尾样本少，仅作盲区例证。')
save(fig,'04_queue_latency','队列条件分位数',['data/independent_evidence.json'],'各桶为独立任务结果，不能用来预测DAG请求p99。')

# 5. Signal choices across workload families, matched within each batch.
fig,axs=plt.subplots(1,3,figsize=(15,6.8),sharey=True)
fig.suptitle('图5  同批策略对照：收益随负载变化，计数信号没有通用赢家',x=.055,ha='left',fontsize=17)
groups=[('4012',16,'L / burst8 / T16\n3 seed，n=192/格'),('4012',128,'L / burst8 / T128\n3 seed，n=192/格'),('4014',16,'L / regular / T16\n3 seed，n=192/格'),('4016',16,'SD / regular / T16\n1 seed，n=1536/格'),('4016',128,'SD / regular / T128\n1 seed，n=1536/格'),('4020',16,'A1异步 / regular / T16\n1 seed，n=1536/格')]
metrics=[('mean_ms','请求mean变化（%）'),('p99_ms','每轮请求p99变化（%）'),('cpu_per_request_ms','整轮CPU/请求变化（%）')]
for ax,(metric,label) in zip(axs,metrics):
 for gi,(batch,thr,_) in enumerate(groups):
  for pi,policy in enumerate(['queue','active','outstanding']):
   rows=pick(batch,threads=thr,trace=0,policy=policy)
   for j,r in enumerate(rows):
    bases=pick(batch,threads=thr,trace=0,policy='home',seed=r['seed'],scenario=r['scenario']);assert len(bases)==1
    if r.get(metric) is None or bases[0].get(metric) is None:continue
    ax.scatter(pct(r,bases[0],metric),gi+(pi-1)*.15+(j-(len(rows)-1)/2)*.035,color=COL[policy],s=30,alpha=.9)
 ax.axvline(0,c='#647887',lw=1);ax.grid(axis='x',color='#e7ecf0');ax.set_axisbelow(True);ax.set_xlabel(label);ax.set_ylim(len(groups)-.55,-.55)
axs[0].set_yticks(range(len(groups)),[x[2] for x in groups],fontsize=10)
for pol,label in [('queue','Q'),('active','Q+I'),('outstanding','O')]:axs[2].scatter([],[],c=COL[pol],label=label)
axs[2].legend(loc='lower right',frameon=False)
fig.subplots_adjust(left=.19,right=.98,top=.86,bottom=.17,wspace=.12)
foot(fig,'每点=一个同批、同seed、同线程候选-home百分比；均值/尾延迟负值更低，CPU正值更耗。跨行不是到达形态因果对照；3 seed改变工作，不是同工作重复。')
save(fig,'05_signal_tradeoffs','跨负载的信号选择',['4012','4014','4016','4020'],'仅画同批匹配差；不平均各轮p99，无置信区间。')

# 6. Thread budget effect and mechanism.
fig,axs=plt.subplots(1,3,figsize=(14,5.5));fig.suptitle('图6  更多worker不等于更快：callback墙钟增长而CPU接近',x=.055,ha='left',fontsize=17)
for pi,policy in enumerate(['home','queue','active','outstanding']):
 for r in pick('4012',threads=16,trace=0,policy=policy):
  b=pick('4012',threads=128,trace=0,policy=policy,seed=r['seed'])[0]
  axs[0].scatter(pi+(r['seed']-421)*.1,pct(r,b,'mean_ms'),color=COL[policy],s=38)
axs[0].axhline(0,c='#647887',lw=1);axs[0].set_xticks(range(4),['home','Q','Q+I','O']);axs[0].set_ylabel('16相对128线程：mean变化（%）');axs[0].set_title('长工作burst：3 seed，trace0',fontsize=11);grid(axs[0])
for t,color,off in [(16,COL['home'],-.18),(128,COL['outstanding'],.18)]:
 rows=[pick('4013',threads=t,policy=p)[0] for p in ['home','queue','outstanding']]
 for ax,key in [(axs[1],'operator_cpu_mean_ms'),(axs[2],'operator_wall_mean_ms')]:
  vals=[r[key] for r in rows]; bars=ax.bar(np.arange(3)+off,vals,.34,color=color,label=f'{t}线程')
  ax.bar_label(bars,fmt='%.2f',fontsize=9,padding=3)
for ax,title in [(axs[1],'业务callback CPU均值（ms）'),(axs[2],'业务callback墙钟均值（ms）')]:
 ax.set_title(title,fontsize=11);ax.set_xticks(range(3),['home','Q','O']);grid(ax);ax.margins(y=.25)
axs[2].legend(frameon=False,loc='upper left',fontsize=9)
fig.subplots_adjust(top=.83,bottom=.22,wspace=.3)
foot(fig,'左：同批线程干预；右：另一批seed421、trace1机制诊断。CPU与墙钟均非独立runq分解；并非所有工作族或p99都随减线程改善。')
save(fig,'06_thread_budget','线程预算及CPU墙钟',['4012','4013'],'不同批次分面展示，不把trace1机制数直接扣到trace0时延。')

# 7. Scope of follow-up rebalancing.
fig,axs=plt.subplots(1,2,figsize=(12,5));fig.suptitle('图7  all与fork：小幅均值收益不保证尾部排名',x=.06,ha='left',fontsize=17)
for tr,off,color in [(0,-.1,COL['home']),(1,.1,COL['outstanding'])]:
 for j,seed in enumerate([420,421,422]):
  a=pick('4011',seed=seed,trace=tr,strategy='all-parent')[0];b=pick('4011',seed=seed,trace=tr,strategy='fork-parent')[0]
  for ax,k in zip(axs,['mean_ms','p99_ms']):ax.scatter(j+off,pct(a,b,k),s=55,c=color,label=f'trace{tr}' if j==0 else None)
for ax,lab in zip(axs,['请求mean变化（%）','每轮请求p99变化（%）']):
 ax.axhline(0,c='#647887',lw=1);ax.set_xticks(range(3),['seed420','seed421','seed422']);ax.set_ylabel(lab);grid(ax);ax.legend(frameon=False)
fig.subplots_adjust(top=.82,bottom=.22,wspace=.28)
foot(fig,'每点为同seed、同trace的all-fork；长工作burst8、T128、每格192请求/24个相关burst。负值表示all更低；不是总体p99或正式验收。')
save(fig,'07_continuation','后继调度范围',['4011'],'all/fork的trace0/1配对；小样本尾部方向有反转。')

# 8. Direct/zero/delayed async.
fig,axs=plt.subplots(1,3,figsize=(14,5.2));fig.suptitle('图8  同业务工作：异步结构与外部等待分别改变结果',x=.055,ha='left',fontsize=17)
for ax,key,title in zip(axs,['mean_ms','p99_ms','cpu_per_request_ms'],['请求mean（ms）','请求p99（ms）','整轮CPU/请求（ms）']):
 for policy,offset in [('home',-.18),('outstanding',.18)]:
  vals=[pick('4018',scenario=s,trace=0,policy=policy)[0][key] for s in ['D0','A0','A1']]
  bars=ax.bar(np.arange(3)+offset,vals,.34,color=COL[policy],label='O' if policy=='outstanding' else 'home')
  ax.bar_label(bars,fmt='%.2f',padding=3,fontsize=8)
 ax.set_xticks(range(3),['D0\n直接','A0\n50% / 0ms','A1\n50% / 2ms']);ax.set_title(title,fontsize=12);grid(ax);ax.margins(y=.23)
axs[0].legend(frameon=False,ncol=2,fontsize=9)
fig.subplots_adjust(top=.81,bottom=.23,wspace=.22)
foot(fig,'同批seed421、T16、regular96QPS、trace0；每格1536请求。业务工作相同，执行合同不同；A0-D0不是纯add成本，A1-A0不是每请求固定2ms。')
save(fig,'08_async_contracts','异步负载合同',['4018'],'每格一次；CPU统计范围为整轮；D0/A0/A1是有意干预，非重复。')

# 9. Async detailed mechanism; imported exactly from recorded roles.
r4020=json.loads((HERE.parent.parent/'results/review/4020_summary.json').read_text())['runs']
fig,axs=plt.subplots(1,3,figsize=(14,5.8));fig.suptitle('图9  恢复排队减少，主要改投发生在首波业务工作',x=.055,ha='left',fontsize=17)
role_names=['first-wave stage0','ordinary successors','rpc_resume']
for policy,off in [('home',-.18),('outstanding',.18)]:
 flow=r4020[policy+'-trace1']['flow'];roles={r['role']:r for r in flow['roles']}
 vals=[roles[k]['wait_mean_ms'] for k in role_names]
 bars=axs[0].bar(np.arange(3)+off,vals,.34,color=COL[policy],label='O' if policy=='outstanding' else 'home');axs[0].bar_label(bars,fmt='%.3f',padding=3,fontsize=9)
 vals=[flow['async_timing_ms'][k]['p99'] for k in ['source_due_to_fire_ms','resume_executor_wait_ms']]
 bars=axs[1].bar(np.arange(2)+off,vals,.34,color=COL[policy]);axs[1].bar_label(bars,fmt='%.3f',padding=3,fontsize=9)
roles={r['role']:r for r in r4020['outstanding-trace1']['flow']['roles']};vals=[100*roles[k]['source_ne_chosen']/roles[k]['n'] for k in role_names]
bars=axs[2].bar(np.arange(3),vals,.55,color=COL['outstanding']);axs[2].bar_label(bars,fmt='%.2f%%',padding=3,fontsize=9)
for ax in axs:grid(ax);ax.margins(y=.22)
axs[0].set_xticks(range(3),['首波','后继','恢复']);axs[0].set_title('callback等待均值（ms）',fontsize=11);axs[0].legend(frameon=False,ncol=2,fontsize=9)
axs[1].set_xticks(range(2),['source lag','恢复排队']);axs[1].set_title('分别的事件p99（ms）',fontsize=11)
axs[2].set_xticks(range(3),['首波','后继','恢复']);axs[2].set_title('O各角色直接换池比例（%）',fontsize=11)
fig.subplots_adjust(top=.82,bottom=.24,wspace=.25)
foot(fig,'仅展示三类角色，后波/join未全改善。完成源线程与executor共享CPU；路由source是保存的实际父池。各角色改投比例以本角色为分母。')
save(fig,'09_async_mechanism','异步分段与迁移角色',['4020'],'两策略各128809恢复；恢复自身改池仅0.63%，parent因果作用尚未独立隔离。')

# 10. End-to-end trace sensitivity, compared separately for each policy and batch.
fig,axs=plt.subplots(1,2,figsize=(12,5.7));fig.suptitle('图10  观测开关的影响需要按工作族和策略分别测',x=.06,ha='left',fontsize=17)
labels=['长burst / home','长burst / fork','长burst / all','短低密 / home T16','短低密 / home T128','A1 / home','A1 / O']
selectors=[('4011','home',128),('4011','fork-parent',128),('4011','all-parent',128),('4015','home',16),('4015','home',128),('4020','home',16),('4020','outstanding',16)]
for gi,(batch,strategy,thr) in enumerate(selectors):
 for r in pick(batch,strategy=strategy,threads=thr,trace=1):
  bases=pick(batch,strategy=strategy,threads=thr,trace=0,seed=r['seed'],scenario=r['scenario']);assert len(bases)==1
  for ax,key in zip(axs,['cpu_per_request_ms','p99_ms']):ax.scatter(pct(r,bases[0],key),gi+(r['seed']-421)*.07,c=COL.get(strategy,COL['home']),s=35)
for ax,lab in zip(axs,['trace1-trace0：整轮CPU/请求（%）','trace1-trace0：请求p99（%）']):
 ax.axvline(0,c='#647887',lw=1);ax.set_ylim(6.5,-.5);ax.set_xlabel(lab);ax.grid(axis='x',color='#e7ecf0')
axs[0].set_yticks(range(7),labels);axs[1].set_yticks([]);fig.subplots_adjust(left=.23,top=.83,bottom=.2,wspace=.16)
foot(fig,'各点均为同批配对。trace0仍维护基础计数与生命周期；差值包含执行反馈和运行波动，不等于纯探针或整个总池开销。')
save(fig,'10_observation','观测开关敏感性',['4011','4015','4020'],'展示所有匹配点，不把home观测比例直接套到O。')

# 11. Historical trace: empirical means/peaks and shared-information correlations.
t=json.loads((HERE/'data/trace_evidence.json').read_text());rows=t['per_pool']
fig,axs=plt.subplots(1,2,figsize=(13,5.5));fig.suptitle('图11  历史轨迹支持短时集中性，不能给出cluster阈值',x=.06,ha='left',fontsize=17)
x=np.arange(4)
for k,off,col,lab in [('mean_oncpu_equivalents',-.18,COL['home'],'10s平均'),('peak_1ms_oncpu_equivalents',.18,COL['outstanding'],'各池1ms桶峰')]:
 bars=axs[0].bar(x+off,[r[k] for r in rows],.34,color=col,label=lab);axs[0].bar_label(bars,fmt='%.1f',fontsize=9,padding=2)
axs[0].set_xticks(x,['历史池a','历史池b','历史池c','历史池d']);axs[0].set_ylabel('逻辑CPU时间当量');axs[0].legend(frameon=False,fontsize=9);axs[0].margins(y=.2);grid(axs[0])
corr=t['full_window_pearson_lag_zero_ranges_across_pools'];labs=['waking→CPU','unique→CPU']
for j,rel in enumerate([('waking','oncpu'),('unique_oncpu','oncpu')]):
 for bin_ms,off,col in [(1,-.12,COL['home']),(10,.12,COL['outstanding'])]:
  r=next(r for r in corr if (r['x'],r['y'])==rel and r['bin_ms']==bin_ms)
  axs[1].plot([r['min'],r['max']],[j+off,j+off],c=col,lw=6,solid_capstyle='round',label=f'{bin_ms}ms窗口' if j==0 else None)
  axs[1].text(r['max']+.018,j+off,f"{r['min']:.3f}–{r['max']:.3f}",va='center',fontsize=9)
axs[1].set_yticks(range(2),labs);axs[1].set_xlim(.4,1.13);axs[1].set_xticks([.4,.6,.8,1]);axs[1].set_ylim(1.5,-.5);axs[1].set_xlabel('同窗Pearson r；线段为四池范围');axs[1].legend(frameon=False,fontsize=9,loc='lower left');axs[1].grid(axis='x',color='#e7ecf0')
fig.subplots_adjust(top=.81,bottom=.24,wspace=.3)
foot(fig,'历史四池无CPU绑定，不是目标架构。各池峰值不同时，不可相加；unique与CPU共用运行区间信息。轨迹覆盖/时钟未知，缺request/operator标记。')
save(fig,'11_historical_trace','历史真实轨迹的可用线索',['data/trace_evidence.json'],'原始trace未在本地重算；不按16 CPU换算绑定后时延，不据此设3ms门控。')
(OUT/'manifest.json').write_text(json.dumps(MANIFEST,ensure_ascii=False,indent=2)+'\n')
print(f'Rendered {len(MANIFEST)} figures to {OUT}')
