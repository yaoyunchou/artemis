import { Component, OnInit, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { AgentService } from '../../services/agent.service';

interface PersonalScript {
  kind: string;
  seconds: number;
  start_button: string;
  done_button: string;
  package?: string | null;
}

interface AppScriptVersion {
  id: string;
  version: number;
  reason: string;
  status: string;
  created_at: number;
  run_count: number;
  last_completed?: number | null;
  last_failed?: number | null;
  last_entered?: number | null;
}

interface AppScriptInfo {
  package: string;
  current: AppScriptVersion | null;
  versions: AppScriptVersion[];
}

const DEFAULT_PACKAGE = 'com.taobao.idlefish';

interface PersonalSubtask {
  id: string;
  title: string;
  prompt: string;
  runner: string;
  script: PersonalScript | null;
  edited: boolean;
  last_status?: string | null;
  last_reason?: string | null;
  last_finished_at?: number | null;
  fail_count?: number;
}

interface PersonalLog {
  id: string;
  title: string;
  runner: string;
  status: string;
  reason: string;
  log_text: string;
}

interface PersonalRun {
  id: string;
  status: string;
  summary: string;
  started_at: number;
  logs: PersonalLog[];
}

interface PersonalTask {
  id: string;
  title: string;
  prompt: string;
  subtasks: PersonalSubtask[];
  runs?: PersonalRun[];
}

@Component({
  selector: 'app-library',
  standalone: true,
  imports: [FormsModule, RouterLink],
  templateUrl: './library.component.html',
  styleUrl: './library.component.scss',
  changeDetection: ChangeDetectionStrategy.Eager
})
export class LibraryComponent implements OnInit {
  private readonly agent = inject(AgentService);

  public readonly tasks = signal<PersonalTask[]>([]);
  public readonly selectedId = signal<string | null>(null);
  public readonly draftPrompt = signal('');
  public readonly editingSubtaskId = signal<string | null>(null);
  public readonly draftSubtaskPrompt = signal('');
  public readonly runs = signal<PersonalRun[]>([]);
  public readonly openLogId = signal<string | null>(null);
  public readonly message = signal<string | null>(null);
  public readonly busy = signal(false);
  public readonly appScript = signal<AppScriptInfo | null>(null);
  public readonly openVersionId = signal<string | null>(null);
  public readonly versionDiff = signal('');

  public ngOnInit(): void {
    this.reload();
  }

  public selected(): PersonalTask | null {
    const id = this.selectedId();
    return this.tasks().find((task) => task.id === id) || null;
  }

  public select(task: PersonalTask): void {
    this.selectedId.set(task.id);
    this.draftPrompt.set(task.prompt);
    this.editingSubtaskId.set(null);
    this.openLogId.set(null);
    this.loadRuns(task.id);
    this.loadAppScript(task);
  }

  public statusLabel(status: string): string {
    const labels: Record<string, string> = {
      current: '当前',
      retired: '旧版',
      rejected: '没通过检查',
      rolled_back: '已退回'
    };
    return labels[status] || status;
  }

  public resultLabel(item: AppScriptVersion): string {
    if (!item.run_count) {
      return '还没跑过';
    }
    const entered = item.last_entered ? '' : '，没进列表';
    return `跑了 ${item.run_count} 次，最近完成 ${item.last_completed || 0} 条、失败 ${item.last_failed || 0} 条${entered}`;
  }

  public toggleVersion(item: AppScriptVersion): void {
    const info = this.appScript();
    if (!info) {
      return;
    }
    if (this.openVersionId() === item.id) {
      this.openVersionId.set(null);
      return;
    }
    this.openVersionId.set(item.id);
    this.versionDiff.set('读取中…');
    this.agent.getAppScriptVersion(info.package, item.id).subscribe({
      next: (payload) => this.versionDiff.set(payload.diff || '和上一版没有差异，或这是第一版。'),
      error: () => this.versionDiff.set('读取这一版失败。')
    });
  }

  public rollbackTo(item: AppScriptVersion): void {
    const info = this.appScript();
    if (!info || this.busy()) {
      return;
    }
    this.busy.set(true);
    this.agent.rollbackAppScript(info.package, item.id).subscribe({
      next: (payload: AppScriptInfo) => {
        this.appScript.set(payload);
        this.busy.set(false);
        this.message.set(`App 脚本已退回第 ${item.version} 版。`);
      },
      error: (err) => {
        this.busy.set(false);
        this.message.set(err?.error?.detail || '退回失败。');
      }
    });
  }

  public savePrompt(): void {
    const task = this.selected();
    const prompt = this.draftPrompt().trim();
    if (!task || !prompt) {
      return;
    }
    this.busy.set(true);
    this.agent.updatePersonalTask(task.id, prompt).subscribe({
      next: (updated: PersonalTask) => {
        this.replaceTask(updated);
        this.busy.set(false);
        this.message.set('提示词已保存，小任务已按新提示词重新拆分。');
      },
      error: () => {
        this.busy.set(false);
        this.message.set('保存提示词失败。');
      }
    });
  }

  public deleteSubtask(subtask: PersonalSubtask): void {
    const task = this.selected();
    if (!task || this.busy()) {
      return;
    }
    this.busy.set(true);
    this.agent.deletePersonalSubtask(task.id, subtask.id).subscribe({
      next: (updated: PersonalTask) => {
        this.replaceTask(updated);
        this.busy.set(false);
        this.message.set(`已删除「${subtask.title}」。`);
      },
      error: () => {
        this.busy.set(false);
        this.message.set('删除小任务失败。');
      }
    });
  }

  public isSkip(subtask: PersonalSubtask): boolean {
    const text = `${subtask.prompt || ''}\n${subtask.script?.kind || ''}`;
    return text.includes('直接跳过') || subtask.script?.kind === 'skip' || (subtask.fail_count || 0) >= 3;
  }

  public recordLabel(subtask: PersonalSubtask): string {
    if (!subtask.last_status) {
      return '今天还没跑';
    }
    const status = subtask.last_status === 'completed'
      ? '已完成'
      : subtask.last_status === 'failed'
        ? '失败'
        : subtask.last_status === 'skipped'
          ? '已跳过'
          : subtask.last_status;
    const when = subtask.last_finished_at ? this.whenLabel(subtask.last_finished_at) : '';
    return when ? `${status} · ${when}` : status;
  }

  public beginSubtaskEdit(subtask: PersonalSubtask): void {
    this.editingSubtaskId.set(subtask.id);
    this.draftSubtaskPrompt.set(subtask.prompt);
  }

  public saveSubtask(): void {
    const task = this.selected();
    const subtaskId = this.editingSubtaskId();
    const prompt = this.draftSubtaskPrompt().trim();
    if (!task || !subtaskId || !prompt) {
      return;
    }
    this.busy.set(true);
    this.agent.updatePersonalSubtask(task.id, subtaskId, prompt).subscribe({
      next: (updated: PersonalTask) => {
        this.replaceTask(updated);
        this.editingSubtaskId.set(null);
        this.busy.set(false);
        this.message.set('小任务提示词已保存。');
      },
      error: () => {
        this.busy.set(false);
        this.message.set('保存小任务失败。');
      }
    });
  }

  public runSelected(): void {
    const task = this.selected();
    if (!task) {
      return;
    }
    this.busy.set(true);
    this.agent.runPersonalTask(task.id).subscribe({
      next: () => {
        this.busy.set(false);
        this.message.set('已加入队列。脚本小任务 60 秒、模型小任务 120 秒，超时会跳过并记下原因。');
        this.loadRuns(task.id);
        this.loadAppScript(task);
      },
      error: () => {
        this.busy.set(false);
        this.message.set('提交执行失败。');
      }
    });
  }

  public toggleLog(logId: string): void {
    this.openLogId.set(this.openLogId() === logId ? null : logId);
  }

  public scriptLabel(subtask: PersonalSubtask): string {
    if (subtask.runner !== 'script' || !subtask.script) {
      return '模型';
    }
    const script = subtask.script;
    const dwell = script.seconds ? `${script.seconds} 秒` : script.kind;
    return `脚本 · ${script.start_button} → ${script.done_button} · ${dwell}`;
  }

  private whenLabel(finishedAt: number): string {
    const date = new Date(finishedAt * 1000);
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    const clock = `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`;
    return sameDay ? `今天 ${clock}` : `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
  }

  private reload(): void {
    this.agent.listPersonalTasks().subscribe({
      next: (payload) => {
        const tasks = payload.tasks || [];
        this.tasks.set(tasks);
        if (!this.selectedId() && tasks.length > 0) {
          this.select(tasks[0]);
        } else if (this.selectedId()) {
          const current = tasks.find((task) => task.id === this.selectedId());
          if (current) {
            this.draftPrompt.set(current.prompt);
          }
        }
      },
      error: () => this.message.set('读取个人收录失败。')
    });
  }

  private loadRuns(taskId: string): void {
    this.agent.listPersonalRuns(taskId).subscribe({
      next: (payload) => this.runs.set(payload.runs || []),
      error: () => this.runs.set([])
    });
  }

  private loadAppScript(task: PersonalTask): void {
    const packageName = task.subtasks.find((item) => item.script?.package)?.script?.package || DEFAULT_PACKAGE;
    this.openVersionId.set(null);
    this.agent.getAppScript(packageName).subscribe({
      next: (payload: AppScriptInfo) => this.appScript.set(payload),
      error: () => this.appScript.set(null)
    });
  }

  private replaceTask(updated: PersonalTask): void {
    this.tasks.set(this.tasks().map((task) => (task.id === updated.id ? updated : task)));
    this.draftPrompt.set(updated.prompt);
  }
}
