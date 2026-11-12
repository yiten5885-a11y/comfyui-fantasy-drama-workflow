import { app } from '/scripts/app.js';
import { api } from '/scripts/api.js';
import { ComfyWidgets } from '/scripts/widgets.js';
app.registerExtension({
  name: 'drama.production.progress',
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!['DramaRenderEpisode', 'DramaProductionSubmit', 'DramaBatchRun'].includes(nodeData.name)) return;
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      original?.apply(this, arguments);
      const projectName = () => {
        const direct = this.widgets.find(w => w.name === 'project')?.value;
        if (direct) return direct;
        const input = this.inputs.find(i => i.name === 'storyboard');
        const link = input && this.graph?.links[input.link];
        const source = link && this.graph.getNodeById(link.origin_id);
        return source?.widgets.find(w => w.name === 'project')?.value || '';
      };
      const progressButton = this.addWidget('button', '打开制作进度 / 下载成片', null, () => window.open('/drama/production?project=' + encodeURIComponent(projectName()), '_blank'));
      progressButton.serialize = false;
      progressButton.options.serialize = false;
      const status = ComfyWidgets.STRING(this, '制作进度', ['STRING', {multiline: true}], app).widget;
      status.inputEl.readOnly = true;
      status.serialize = false;
      status.options.serialize = false;
      status.value = '正在读取工作流与制作状态。';
      const labels = {queued:'排队中',running:'运行中',needs_attention:'需要处理',stopped:'已停止',complete:'已完成',preflight:'模型校验',image:'生成关键帧',image_review:'关键帧复检',video:'生成动态镜头',video_review:'动态视频复检',voice:'生成逐句配音',subtitles:'时间轴与字幕',audio:'混音',compose:'竖屏合成',final_review:'成片质检',publish:'复制交付成片'};
      const update = async () => {
        if (!this.graph) return;
        try {
          const responses = await Promise.all(['/drama/build', '/drama/jobs', '/drama/models'].map(path => api.fetchApi(path)));
          if (responses.some(r => !r.ok)) throw Error('读取制作状态失败');
          const [gate, data, models] = await Promise.all(responses.map(r => r.json()));
          const job = data.jobs.find(j => j.project === projectName());
          if (!gate.inference_allowed) {
            status.value = '自动推理已锁定\n' + gate.reason;
            if (job) status.value += '\n历史任务：' + (labels[job.status] || job.status);
          } else {
            status.value = job ? `${labels[job.status] || job.status} · ${labels[job.phase] || job.phase}\n${job.current_shot || ''}\n${job.error || ''}\n任务 ${job.job_id}` : '还未提交。检查设置后点击右上角运行。';
          }
          if (models.status === 'verified') status.value += '\n视频模型文件：已校验，按任务加载';
          else if (models.total) status.value += `\n模型下载 ${(models.downloaded/1e9).toFixed(1)} / ${(models.total/1e9).toFixed(1)} GB`;
          app.graph.setDirtyCanvas(true);
        } catch (e) { status.value = e.message; }
      };
      const timer = setInterval(update, 5000);
      const removed = this.onRemoved;
      this.onRemoved = function () { clearInterval(timer); removed?.apply(this, arguments); };
      this.setSize([500, 350]);
      update();
    };
  }
});
