import { splitNumberedTask } from './split-task';

describe('splitNumberedTask', () => {
  it('keeps the preamble on each row and drops the other items', () => {
    const text = '规则：不要掷骰子。\n1. 去蚂蚁庄园逛一逛\n停留 20 秒\n2. 看视频领奖励';
    const items = splitNumberedTask(text);

    expect(items.length).toBe(2);
    expect(items[0].goal).toContain('不要掷骰子');
    expect(items[0].goal).toContain('去蚂蚁庄园逛一逛');
    expect(items[0].goal).toContain('停留 20 秒');
    expect(items[0].goal).not.toContain('看视频领奖励');
    expect(items[1].title).toBe('看视频领奖励');
    expect(items[1].goal).not.toContain('去蚂蚁庄园逛一逛');
  });

  it('returns no rows when the prompt is a single paragraph', () => {
    expect(splitNumberedTask('打开闲鱼并领取金币')).toEqual([]);
  });

  it('accepts Chinese enumeration marks', () => {
    const items = splitNumberedTask('1、第一件\n2、第二件');
    expect(items.map((item) => item.title)).toEqual(['第一件', '第二件']);
  });
});
