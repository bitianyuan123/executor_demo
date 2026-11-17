#!/usr/bin/env python3
"""Concrete execution diagrams and within-workload comparisons from reviewed data."""
from pathlib import Path
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE=Path(__file__).resolve().parent
FONT=Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
font_manager.fontManager.addfont(str(FONT))
plt.rcParams.update({'font.family':font_manager.FontProperties(fname=str(FONT)).get_name(),
 'font.size':11,'axes.unicode_minus':False,'axes.spines.top':False,'axes.spines.right':False,
 'axes.labelcolor':'#25384a','text.color':'#25384a','svg.fonttype':'path','savefig.facecolor':'white'})
COL={'home':'#355C83','queue':'#C37C1E','active':'#8764AC','outstanding':'#188B83','fork-parent':'#C37C1E','all-parent':'#188B83'}
RUNS=json.loads((HERE/'data/comparisons.json').read_text())['runs']
OUT=HERE/'figures';OUT.mkdir(exist_ok=True);MANIFEST=[]
def save(fig,name,title,sources,caption):
 for ext in ['png','svg']:fig.savefig(OUT/f'{name}.{ext}',dpi=180,bbox_inches='tight',pad_inches=.2)
 p=OUT/f'{name}.svg';p.write_text('\n'.join(l.rstrip() for l in p.read_text().splitlines())+'\n')
 MANIFEST.append(dict(name=name,title=title,sources=sources,caption=caption));plt.close(fig)
def diagram(title,sub='',size=(14,7)):
 fig,ax=plt.subplots(figsize=size);ax.set_xlim(-2,102);ax.set_ylim(-3,103);ax.axis('off')
 fig.suptitle(title,x=.045,ha='left',fontsize=17,y=.98)
 fig.text(.045,.917,sub,fontsize=10,color='#506678');fig.subplots_adjust(top=.86,bottom=.08)
 return fig,ax
def box(ax,x,y,w,h,text,color='#e9f0f6',fs=11):
 ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=.35,rounding_size=.7',fc=color,ec='#9bacbb'))
 ax.text(x+w/2,y+h/2,text,ha='center',va='center',fontsize=fs,linespacing=1.5)
def arrow(ax,a,b,label='',color='#536e84',offset=(0,1)):
 ax.add_patch(FancyArrowPatch(a,b,arrowstyle='-|>',mutation_scale=13,color=color,lw=1.5))
 if label:ax.text((a[0]+b[0])/2+offset[0],(a[1]+b[1])/2+offset[1],label,ha='center',va='bottom',fontsize=10,bbox=dict(fc='white',ec='none',pad=1))
def footer(fig,t):fig.text(.045,.015,t,fontsize=9,color='#546878')
def pick(batch,**kwargs):return [r for r in RUNS if r['batch']==str(batch) and all(r.get(k)==v for k,v in kwargs.items())]
def grid(ax):ax.grid(axis='y',color='#e5ecf1');ax.set_axisbelow(True)

# Planned arrivals, deliberately not pretending to be observed timelines.
fig,axs=plt.subplots(3,1,figsize=(13,6.2));fig.suptitle('图1  请求何时进入demo：三种明确的计划到达',x=.06,ha='left',fontsize=17)
for ax,q,burst,title in [(axs[0],12,False,'12 QPS均匀：每83.333ms一个请求；同home每500ms一次'),(axs[1],12,True,'12 QPS成组：8个请求相隔100µs；每666.667ms一组'),(axs[2],96,False,'96 QPS均匀：每10.417ms一个请求；同home每62.5ms一次')]:
 x=np.arange(8)*(.1 if burst else 1000/q)
 ax.hlines(0,-.03*x[-1],x[-1]*1.03,color='#9aabb8')
 ax.vlines(x,-.12,.12,color=COL['outstanding'],lw=2)
 for i,v in enumerate(x):ax.text(v,.2,f'R{i}→池{i%6}',ha='center',fontsize=10)
 ax.set_title(title,fontsize=12,loc='left');ax.set_ylim(-.3,.7);ax.set_yticks([]);ax.set_xticks(x,[f'{v:.3f}' if not burst else f'{v:.1f}' for v in x]);ax.set_xlabel('相对首请求的计划时间（ms）');ax.spines['left'].set_visible(False)
fig.subplots_adjust(top=.83,bottom=.13,hspace=1.15)
footer(fig,'只画前8个计划到达；真实投递可能落后。RR仅确定home；它没有决定该请求全部算子的最终执行池。')
save(fig,'01_arrivals','请求计划到达',['experiments/affinity/main.cpp run'],'计划示意，不是从真实CPU或投递时间序列重建。')

fig,ax=diagram('图2  展开一条请求：168个业务算子，并非一次提交168个','本图展示无异步基础路径。生产中的IO接收/worker构图被一个计划到达发生器替代。',size=(15,8))
box(ax,1,84,21,14,'发生器到计划时间\nbeginRequest(R0)\nRR固定home=池0',fs=11)
box(ax,29,84,30,14,'仅提交1个初始化callback\npinHome=true → 池0\n首次触碰84×64KiB = 5.25MiB',color='#e5f1ee',fs=11)
box(ax,68,84,30,14,'初始化callback中 fork(0)\n生成80个第一段闭包\naddBatch；chunk=1逐个选池',color='#fff0d9',fs=11)
arrow(ax,(22,91),(29,91));arrow(ax,(59,91),(68,91))
arrow(ax,(83,84),(83,76));arrow(ax,(83,76),(-1,76));ax.plot([-1,-1],[29,76],color='#536e84',lw=1.5)
for branch_y in [63,46,29]:arrow(ax,(-1,branch_y),(2,branch_y))
for y,lab in [(58,'分支0'),(41,'分支1'),(24,'分支79')]:
 box(ax,2,y,23,10,f'{lab} 第一段算子',fs=11)
 box(ax,37,y,23,10,f'{lab} 第二段算子',fs=11)
 arrow(ax,(25,y+5),(37,y+5),'单独add',offset=(0,2))
 ax.text(31,y-3,'同一64KiB读写数据',ha='center',fontsize=9,color='#188b83')
 arrow(ax,(60,y+5),(68,46))
ax.text(12,37,'…',fontsize=18,ha='center');ax.text(48,37,'…',fontsize=18,ha='center')
box(ax,69,35,29,22,'80条两段链全完成\n最后完成者提交join\njoin再fork(1)\n4条新的两段链',color='#fff0d9',fs=11)
arrow(ax,(83,35),(83,18));box(ax,68,2,30,16,'4×2=8个算子 → 最终join\n读取各分支checksum\n标记请求完成',color='#e5f1ee',fs=11)
box(ax,2,1,57,13,'第一段结束计算即可提交本分支第二段，不等其他79支。\n1初始化 + 160第一波算子 + 1join + 8第二波算子 + 1join = 171基础callback。',fs=10)
footer(fig,'每个算子都通过总池add/addBatch；子callback可在父callback返回前开始。第二波用4个新buffer，只有控制依赖，不复用第一波payload。')
save(fig,'02_request_dag','一个请求的完整DAG',['experiments/affinity/main.cpp fork/stage/advance','LocalityWorkload.h'],'80个首波第一段依次投递；深度2不构成跨分支的stage barrier。')

fig,ax=diagram('图3  Q / I / O / R：沿一次算子提交逐点定位','橙色是总池/包装器维护；蓝色是Folly原生状态；绿色是callback生命周期。',size=(15,8.6))
ax.text(24,98,'提交者：driver / 算子worker / helper',ha='center',fontsize=13)
ax.text(77,98,'消费者：目标池worker',ha='center',fontsize=13)
steps=[(81,'A. choose()读取候选池\nQ=getTaskQueueSize；I/O=包装计数；R=总池预留','#e8f0f7'),(65,'B. 选定目标j后：R[j] += chunk\n当前chunk=1，不预留整张图','#fff0d9'),(49,'C. trace=1时记Q/I/O_before及t_submit\n随后BusinessExecutor::add：O[j] += 1','#fff0d9'),(33,'D. Folly::add：闭包入队，再post\nQ是信号量/队列接口的近似量','#e8f0f7'),(14,'E. 底层add返回后：R[j] -= 1\n拒绝/异常走回滚，不遗留占用','#fff0d9')]
for y,t,c in steps:box(ax,1,y,47,12,t,c,10.5)
for a,b in [(81,77),(65,61),(49,45),(33,26)]:arrow(ax,(24,a),(24,b))
box(ax,55,74,44,16,'worker被唤醒后由内核调度\n或已活跃worker继续取任务\n取得permit使原生Q变化，再dequeue',fs=11)
arrow(ax,(48,39),(55,80),'通知/可取任务',offset=(0,3))
box(ax,55,54,44,13,'F. 包装callback开始：I[j] += 1\n安装request/parent；记t_start',color='#e2f2ed',fs=11);arrow(ax,(77,74),(77,67))
box(ax,55,30,44,17,'G. 执行本算子\n计算 → 提交本分支后继 / 注册异步等待\n即使被抢占或同步阻塞，I/O仍保留',color='#e2f2ed',fs=11);arrow(ax,(77,54),(77,47))
box(ax,55,7,44,15,'H. 记t_finish，callback返回\nI[j] -= 1；O[j] -= 1\n异常也释放；外部pending另计',color='#e2f2ed',fs=11);arrow(ax,(77,30),(77,22))
footer(fig,'D后左右两列并发：callback可能在E之前开始甚至结束。Q不是Linux runq；I不是on-CPU。submit→start不含A/B选池预留。')
save(fig,'03_observation_sites','观测点定位',['RequestExecutor.h submit/batch/enqueue','BusinessExecutor.h add/readLoad'],'R在底层add返回后释放，可与O短时重叠；I/O可同时包含父子callback。')

# Frozen-state teaching example, not a performance simulation.
loads=[1,0,0];history=[loads.copy()];dest=[]
for j in range(80):
 selected=0
 if loads[0]>16:
  for candidate in [1,2]:
   if loads[candidate]<loads[selected] and loads[0]-loads[candidate]>0:selected=candidate
 loads[selected]+=1;dest.append(selected);history.append(loads.copy())
assert [dest.count(j) for j in range(3)]==[26,27,27]
fig,axs=plt.subplots(1,2,figsize=(14,5.6));fig.suptitle('图4  同一个请求的80个首波算子确实会跨池分配',x=.055,ha='left',fontsize=17)
for i,col in enumerate([COL['home'],COL['outstanding'],COL['active']]):axs[0].step(range(81),np.array(history)[:,i],where='post',label=f'池{i}',color=col)
axs[0].axhline(16,ls='--',color='#8a9cab');axs[0].set_xlabel('已投递的首波算子数');axs[0].set_ylabel('未完成callback O（含初始化）');axs[0].legend(frameon=False);grid(axs[0])
for i,col in enumerate([COL['home'],COL['outstanding'],COL['active']]):
 xs=[j+1 for j,g in enumerate(dest) if g==i];axs[1].scatter(xs,[i]*len(xs),color=col,s=26)
axs[1].set_yticks([0,1,2],['池0：26个算子','池1：27个算子','池2：27个算子']);axs[1].set_xlabel('首波算子序号1～80');axs[1].set_ylim(2.5,-.5);axs[1].grid(axis='x',color='#e5ecf1')
fig.subplots_adjust(top=.8,bottom=.25,wspace=.35)
footer(fig,'教学演算：同NUMA三池、C16、high1、gain0、chunk1；只让home初始化继续提交，暂不消费子任务，无其他请求。实际运行不是这个固定分配。\n第1～16个留home；第17个开始借出；home不随算子改投而改变。R每次交接释放，图中显示交接后的O。')
save(fig,'04_operator_placement','80算子逐次选池演算',['RequestExecutor.h choose/batch'],'明确假设下的教学状态机，不是测得的80任务分配或性能预测。')

fig,ax=diagram('图5  一条分支的续接：保留home，但parent跟随实际执行池','示例：request home=池0，第一段算子被借到同NUMA的池1。',size=(14,6.5))
box(ax,2,75,29,17,'第一段算子在池1执行\n保存：home=0，source=1\n计算并修改本分支64KiB',color='#e2f2ed')
box(ax,38,75,29,17,'直接路径：在callback内add\n第二段以parent=1为偏好\n再次按fork/all与负载判断',fs=10.5);arrow(ax,(31,84),(38,84))
box(ax,75,75,24,17,'pool1不忙：仍池1\n忙且池2更轻：可池2\nhome仍是池0',fs=10.5);arrow(ax,(67,84),(75,84))
box(ax,2,39,29,17,'异步路径：保存续接token\ndue=登记时刻+2ms\n注册事件；返回时退出I/O',color='#fff0d9',fs=10.5);arrow(ax,(16,75),(16,56))
box(ax,38,39,29,17,'helper到期派发恢复callback\n恢复的是home0 / source1\n不会把helper所在CPU当parent',fs=10.5);arrow(ax,(31,47),(38,47))
box(ax,75,39,24,17,'恢复callback经总池选池\n执行advance → add第二段\n第二段继承恢复实际池',fs=10.5);arrow(ax,(67,47),(75,47))
box(ax,8,2,84,22,'parent：逐分支跟随实际前驱；不是给整个请求改一个“当前池”。\ncontinuation=home：未被fork/hop拦截且home≤low、域允许时，优先回home。\nfork-only：首波和第二波批量投递可借出，普通第二段/恢复/join保持parent；all：这些提交也可重新判断。',fs=11)
footer(fig,'helper派发与原callback返回无先后保证。2ms不是请求总时延固定加2ms；外部pending不加入CPU池O。')
save(fig,'05_successor_async','分支后继与异步续接',['main.cpp stage/awaitCompletion/advance','RequestExecutor.h choose'],'parent和ReturnHome不同；异步保存实际父池，不继承完成源线程位置。')

def scenario_chart(name,number,batch,title,sub,threads,policies):
 fig,axs=plt.subplots(len(threads),3,figsize=(14,4.1*len(threads)+1),squeeze=False)
 fig.suptitle(f'图{number}  '+title,x=.05,ha='left',fontsize=16);fig.text(.05,.91,sub,fontsize=10)
 for ri,t in enumerate(threads):
  for ci,(key,lab) in enumerate([('mean_ms','请求均值（ms）'),('p99_ms','每轮请求p99（ms）'),('cpu_per_request_ms','整轮CPU/请求（ms）')]):
   ax=axs[ri,ci]
   for pi,pol in enumerate(policies):
    rr=pick(batch,threads=t,trace=0,strategy=pol) if pol.endswith('parent') else pick(batch,threads=t,trace=0,policy=pol)
    for j,r in enumerate(rr):
     dx=(j-(len(rr)-1)/2)*.12
     ax.scatter(pi+dx,r[key],color=COL[pol],s=42,marker={420:'o',421:'s',422:'^'}[r['seed']])
     if len(rr)==1:ax.annotate(f'{r[key]:.2f}',(pi,r[key]),xytext=(0,8),textcoords='offset points',ha='center',fontsize=9)
   ax.set_xticks(range(len(policies)),[{'home':'固定home','queue':'Q选池','active':'Q+I选池','outstanding':'O选池','fork-parent':'仅fork','all-parent':'全部提交'}[p] for p in policies],fontsize=10)
   ax.set_title(f'每池{t}线程 · {lab}',fontsize=11);grid(ax);ax.margins(x=.16,y=.2)
 fig.subplots_adjust(top=.84 if len(threads)==1 else .85,bottom=.15 if len(threads)==1 else .11,wspace=.27,hspace=.45)
 seed_note='仅seed421，每格一次。' if batch=='4016' else '圆/方/三角分别为seed420/421/422；各格一次、同seed配对。'
 footer(fig,seed_note+'CPU/请求是计算消耗，不是实时CPU利用率。\n请求时延取计划到达[2,18)窗口，CPU取整轮；该批实时CPU未采集，迁移记录须看单独trace1对照。')
 save(fig,name,title,[batch],'场景内绝对指标，明确负载与线程；没有填造缺失的CPU时间序列。')
scenario_chart('06_burst12_3m',6,'4012','12 QPS：8请求一组，组内间隔100µs，批周期666.667ms','每请求80×2+4×2=168算子；基准300万迭代+10万读写/算子，±25%及约1/8四倍；6个cluster池，无异步。',[16,128],['home','queue','active','outstanding'])
scenario_chart('08_regular12_3m',8,'4014','12 QPS，每83.333ms一请求：比较Q与O','每请求80×2+4×2=168算子；基准300万迭代+10万读写/算子，±25%及约1/8四倍；6个cluster池，无异步。',[16],['home','queue','outstanding'])
scenario_chart('09_regular96_375k',9,'4016','96 QPS，每10.417ms一请求：比较Q与O','每请求80×2+4×2=168算子；基准37.5万迭代+1.25万读写/算子，±25%及约1/8四倍；6个cluster池，无异步。',[16,128],['home','queue','outstanding'])
scenario_chart('10_fork_all',10,'4011','12 QPS、8请求成组、每池128线程：算子在哪些提交点均衡','每组跨度700µs、周期666.667ms；168算子/请求；300万迭代+10万读写/算子；trace0、O评分、parent、同NUMA借出。',[128],['home','fork-parent','all-parent'])

# Mechanism: same batch/trace, including migration, not spliced into trace0.
d13=json.loads((HERE.parents[1]/'results/review/4013_summary.json').read_text())['runs']
fig,axs=plt.subplots(2,3,figsize=(14,8));fig.suptitle('图7  12 QPS、8请求成组：同一次trace1中的执行、等待和改投',x=.05,ha='left',fontsize=16)
for ri,t in enumerate([16,128]):
 rows=[next(r for r in d13 if r['threads']==t and r['policy']==p) for p in ['home','queue','outstanding']]
 for ci,(key,lab) in enumerate([('operator_cpu_mean_ms','算子CPU / 墙钟均值（ms）'),('task_wait_p99_ms','所有callback提交→开始p99（ms）'),('cohort_migrations','改投次数 / 全部callback（%）')]):
  ax=axs[ri,ci]
  if ci==0:
   for off,k,col,l in [(-.18,'operator_cpu_mean_ms',COL['home'],'CPU'),(.18,'operator_wall_mean_ms',COL['outstanding'],'墙钟')]:
    bars=ax.bar(np.arange(3)+off,[r[k] for r in rows],.34,label=l,color=col);ax.bar_label(bars,fmt='%.2f',fontsize=9,padding=2)
  else:
   vals=[r[key] if ci==1 else r[key]*100/r['cohort_callbacks'] for r in rows]
   bars=ax.bar(range(3),vals,color=[COL[p] for p in ['home','queue','outstanding']]);ax.bar_label(bars,fmt='%.2f',fontsize=9,padding=2)
  ax.set_xticks(range(3),['固定home','Q选池','O选池']);ax.set_title(f'{t}线程 · {lab}',fontsize=11);grid(ax);ax.margins(y=.23)
fig.legend(*axs[0,0].get_legend_handles_labels(),loc='upper center',bbox_to_anchor=(.5,.94),ncol=2,frameon=False)
fig.subplots_adjust(top=.82,bottom=.13,wspace=.3,hspace=.45)
footer(fig,'批内100µs、跨度700µs、批周期666.667ms。同seed421，192请求/32832个callback；300万迭代+10万读写/算子。改投=chosen≠保存的source，不是内核迁移。\n每点/柱来自本批trace1；它与图6的trace0为不同执行，不能把本图迁移率当作图6该轮的实测值。实时CPU未采集。')
save(fig,'07_same_run_mechanism','同轮执行与迁移证据',['4013'],'同时比较CPU/墙钟、开始前等待及改投率；不同统计对象分别标出。')

# Async contracts, numeric labels rather than names alone.
fig,axs=plt.subplots(1,3,figsize=(14,5.3));fig.suptitle('图11  均匀每10.417ms一请求（96 QPS）：外部等待如何改变同一张图',x=.05,ha='left',fontsize=16)
for ax,key,lab in zip(axs,['mean_ms','p99_ms','cpu_per_request_ms'],['请求均值（ms）','请求p99（ms）','整轮CPU/请求（ms）']):
 for p,off in [('home',-.18),('outstanding',.18)]:
  vals=[pick('4018',scenario=s,trace=0,policy=p)[0][key] for s in ['D0','A0','A1']]
  bars=ax.bar(np.arange(3)+off,vals,.34,color=COL[p],label='固定home' if p=='home' else 'O选池');ax.bar_label(bars,fmt='%.2f',fontsize=9,padding=3)
 ax.set_xticks(range(3),['直接续接','50%位置\n源等待0ms','50%位置\n源等待2ms']);ax.set_title(lab,fontsize=12);grid(ax);ax.margins(y=.25)
axs[0].legend(frameon=False,fontsize=9);fig.subplots_adjust(top=.81,bottom=.25,wspace=.25)
footer(fig,'每池16线程、每请求168业务算子，基准37.5万迭代+1.25万读写，数据84×64KiB；同seed421、trace0、每格1536请求。\n50%为逐位置哈希选择，不保证每请求恰好84次；CPU/请求包含恢复控制和helper，非CPU利用率。')
save(fig,'11_async_wait','异步等待对照',['4018'],'同业务工作，改变外部续接合同；每格单次。')

r20=json.loads((HERE.parents[1]/'results/review/4020_summary.json').read_text())['runs']
fig,axs=plt.subplots(1,3,figsize=(14,5.6));fig.suptitle('图12  均匀每10.417ms一请求、50%位置等2ms：改善发生在哪里',x=.05,ha='left',fontsize=16)
roles=['first-wave stage0','ordinary successors','rpc_resume']
for p,off in [('home',-.18),('outstanding',.18)]:
 flow=r20[p+'-trace1']['flow'];idx={r['role']:r for r in flow['roles']}
 bars=axs[0].bar(np.arange(3)+off,[idx[k]['wait_mean_ms'] for k in roles],.34,color=COL[p],label='固定home' if p=='home' else 'O选池');axs[0].bar_label(bars,fmt='%.3f',fontsize=9,padding=2)
 bars=axs[1].bar(np.arange(2)+off,[flow['async_timing_ms'][k]['p99'] for k in ['source_due_to_fire_ms','resume_executor_wait_ms']],.34,color=COL[p]);axs[1].bar_label(bars,fmt='%.3f',fontsize=9,padding=2)
idx={r['role']:r for r in r20['outstanding-trace1']['flow']['roles']}
bars=axs[2].bar(range(3),[idx[k]['source_ne_chosen']*100/idx[k]['n'] for k in roles],color=COL['outstanding']);axs[2].bar_label(bars,fmt='%.2f%%',fontsize=9,padding=2)
axs[0].set_title('callback等待均值（ms）');axs[1].set_title('源/恢复各自p99（ms）');axs[2].set_title('O：各角色改投率（%）')
axs[0].set_xticks(range(3),['首波第一段','普通第二段','恢复控制']);axs[1].set_xticks(range(2),['due→fire','恢复提交→开始']);axs[2].set_xticks(range(3),['首波第一段','普通第二段','恢复控制']);axs[0].legend(frameon=False,fontsize=9)
for ax in axs:grid(ax);ax.margins(y=.22)
fig.subplots_adjust(top=.81,bottom=.24,wspace=.3)
footer(fig,'每池16线程，168业务算子/请求，37.5万迭代+1.25万读写/算子；同批trace1，每策略1536请求。\n改投率分别除以本角色数量；恢复仅812/128809改投。首波占全部改投75.66%，是另一分母。实时CPU未采集。')
save(fig,'12_async_roles','异步角色与改投',['4020'],'源等待与CPU ready排队分开；不将恢复收益归为恢复自身大量改投。')

# Trace diagram tied directly to concrete decision, not a correlation gallery.
t=json.loads((HERE/'data/trace_evidence.json').read_text());rows=t['per_pool']
fig,axs=plt.subplots(1,2,figsize=(13.5,5.3));fig.suptitle('图13  历史未绑核轨迹：平均CPU服务较低，峰值却集中在很短的窗口',x=.05,ha='left',fontsize=16)
for off,k,col,label in [(-.18,'mean_oncpu_equivalents',COL['home'],'10s平均'),(.18,'peak_1ms_oncpu_equivalents',COL['outstanding'],'本池最忙1ms')]:
 bars=axs[0].bar(np.arange(4)+off,[r[k] for r in rows],.34,color=col,label=label);axs[0].bar_label(bars,fmt='%.1f',fontsize=10,padding=2)
axs[0].set_xticks(range(4),['历史池a','历史池b','历史池c','历史池d']);axs[0].set_ylabel('窗口内on-CPU时长 / 窗口长度');axs[0].legend(frameon=False,loc='upper center',bbox_to_anchor=(.5,1.15),ncol=2,fontsize=9);axs[0].margins(y=.2);grid(axs[0])
axs[1].axis('off');axs[1].text(0,.93,'一个可直接理解的例子',fontsize=14,weight='bold');axs[1].text(0,.79,'历史池a：某1ms窗口累计获得78.4ms CPU时间。',fontsize=12);axs[1].text(0,.65,'若只允许16个逻辑CPU，1ms最多提供16ms CPU时间。',fontsize=11);axs[1].text(0,.51,'因此硬绑定会改变可获得的峰值并行能力。',fontsize=12,color=COL['outstanding']);axs[1].text(0,.35,'但：绑定后每算子CPU成本、竞争和缓存可能变化；',fontsize=11);axs[1].text(0,.23,'不能用78.4÷16直接预测请求时延，也不能据此选阈值。',fontsize=11)
fig.subplots_adjust(top=.81,bottom=.21,wspace=.23)
footer(fig,'历史4池仅为无绑定采集配置；目标仍按cluster分组。各池峰不同时，不能相加；运行区间匹配与采集覆盖有缺失。')
save(fig,'13_trace_mean_peak','历史峰值服务与cluster限制',['data/trace_evidence.json'],'逻辑CPU时间当量，不是当前测试实时利用率。')

# A new, independently captured contract. Do not backfill its CPU into 4014.
from cpu_window_metrics import derive, load_rounds
CPU_REVIEW=HERE.parents[1]/'results/review/4021_summary.json'
cpu_review=json.loads(CPU_REVIEW.read_text())
cpu_rounds={r['round_id']:r for r in cpu_review['rounds']}
cpu_rows=load_rounds(CPU_REVIEW.with_name('4021_cpu_windows.csv'))
cpu_metrics={name:derive(rows) for name,rows in cpu_rows.items()}
blocks=[['01-home','02-queue','03-outstanding'],
        ['06-home','05-queue','04-outstanding']]
fig,axs=plt.subplots(3,2,figsize=(15,8.5),sharex=True,sharey=True)
fig.suptitle('图14  每83.333ms一请求、首波80算子：实测六cluster的CPU时间分布',
             x=.06,ha='left',fontsize=17)
fig.text(.06,.925,'每池16线程；168算子/请求，基准300万迭代+10万访问；parent/all、同NUMA、trace1。颜色为实际约100ms窗口平均。',fontsize=10)
for bi,block in enumerate(blocks):
 for pi,name in enumerate(block):
  ax=axs[pi,bi]
  grouped={}
  for r in cpu_rows[name]:
   bounds=(float(r['relative_start_ms'])/1000,float(r['relative_end_ms'])/1000)
   grouped.setdefault(bounds,{})[int(r['cluster'])]=float(r['util_percent'])
  for (start,end),members in sorted(grouped.items()):
   # Preserve real time edges and any boundary gaps; no interpolation.
   mesh=ax.pcolormesh([start,end],np.arange(7)-.5,
       np.array([members[i] for i in range(6)]).reshape(6,1),
       vmin=0,vmax=100,cmap='YlOrRd',shading='flat',rasterized=True)
  ax.axhline(2.5,color='#25384a',lw=.9)
  ax.set_title(f'比较块{bi+1} · {name}',loc='left',fontsize=12)
  ax.set_yticks(range(6),['C0','C1','C2','C3','C4','C5'])
  ax.set_ylim(5.5,-.5);ax.set_xlim(2,18)
  if bi==0:ax.set_ylabel('NUMA 0: C0–2\nNUMA 2: C3–5',fontsize=10)
  if pi==2:ax.set_xlabel('相对本轮origin的时间（s）')
fig.subplots_adjust(top=.86,bottom=.17,right=.9,wspace=.12,hspace=.40)
cax=fig.add_axes([.92,.21,.015,.61]);fig.colorbar(mesh,cax=cax,label='cluster逻辑CPU利用率（%）')
footer(fig,'每轮159个完整窗口；六轮独立执行，横轴各自从origin计时，不能逐像素作同步因果比较。C0–2和C3–5分别共享同一NUMA。\n原始采样目标100ms，实际92.8–107.2ms；未画入不足完整窗口的边界。不能从此图推断1ms峰值或30ms持续时间。')
save(fig,'14_cpu_windows','同一负载的逐cluster CPU实测时间窗',
     ['results/review/4021_summary.json','results/review/4021_cpu_windows.csv'],
     '6轮独立执行，固定颜色范围0–100%，按真实采样起止画格，不把callback占用当CPU。')

fig,axs=plt.subplots(2,3,figsize=(15,8.6))
fig.suptitle('图15  同轮比较：局部排队减少，整体CPU消耗基本不变',x=.05,ha='left',fontsize=17)
fig.text(.05,.925,'均匀12QPS；80×2+4×2算子；300万迭代+10万访问/基准算子；6×16线程；同seed421、trace1、100ms外采。',fontsize=10)
panels=[('请求均值（ms）',lambda r,m:r['report']['mean_ms']),
        ('请求p99（ms）',lambda r,m:r['report']['p99_ms']),
        ('整轮进程CPU/请求（ms）',lambda r,m:r['whole_run']['process_cpu_per_request_ms']),
        ('新callback改投率（%）',lambda r,m:100*r['report']['migrations']/r['report']['tasks']),
        ('每窗最忙cluster利用率的p95（%）',lambda r,m:m['max_cluster_util_percent']['p95']),
        ('同NUMA内利用率极差均值（百分点）',lambda r,m:m['mean_local_span_pp'])]
for ax,(label,get) in zip(axs.flat,panels):
 for bi,block in enumerate(blocks):
  for pi,name in enumerate(block):
   r=cpu_rounds[name];v=get(r,cpu_metrics[name]);x=pi+(-.12 if bi==0 else .12)
   ax.scatter(x,v,s=53,color=COL[r['policy']],marker=['o','s'][bi],zorder=3)
   other=blocks[1-bi][pi]
   ov=get(cpu_rounds[other],cpu_metrics[other])
   above=v>ov or (v==ov and bi==0)
   ax.annotate(f'{v:.2f}',(x,v),xytext=(0,9 if above else -16),
               textcoords='offset points',ha='center',fontsize=9)
 ax.set_xticks(range(3),['固定home','Q选池','O选池']);ax.set_title(label,fontsize=12)
 ax.set_xlim(-.45,2.45);ax.margins(y=.32);grid(ax)
fig.subplots_adjust(top=.84,bottom=.16,hspace=.48,wspace=.27)
footer(fig,'圆点=比较块1，方点=比较块2；各192请求，p99各自取第191顺位，不合并或平均。CPU/请求为整轮240请求口径，纵轴放大约0.4%的差异。\n同NUMA极差：每窗分别算两个NUMA的max-min，取两者平均，再按窗长加权。图中下排CPU指标均来自159个实际窗口。')
save(fig,'15_cpu_latency_routing','同轮时延CPU与改投',
     ['results/review/4021_summary.json','results/review/4021_cpu_windows.csv'],
     '分别展示两组三策略比较，不平均p99，不用总CPU成本代替窗口利用率。')

(OUT/'manifest.json').write_text(json.dumps(sorted(MANIFEST,key=lambda item:item['name']),ensure_ascii=False,indent=2)+'\n')
# Remove only earlier generated files tracked in the old report; no user results.
legacy=['01_scene','02_sequence','03_routing','04_queue_latency','05_signal_tradeoffs','06_thread_budget','07_continuation','08_async_contracts','09_async_mechanism','10_observation','11_historical_trace']
for stem in legacy:
 for ext in ['png','svg']:
  p=OUT/f'{stem}.{ext}'
  if p.exists():p.unlink()
print(f'Rendered {len(MANIFEST)} concrete figures')
