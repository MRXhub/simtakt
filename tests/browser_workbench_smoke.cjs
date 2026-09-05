/** Real browser + SQLite + governed runtime smoke test; requires Playwright and Chrome.
 * Run: node tests/browser_workbench_smoke.cjs
 * Optional: PYTHON, BROWSER_CHANNEL. Artifacts are retained under tmp/browser-smoke-*.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {spawn} = require("node:child_process");
const readline = require("node:readline");
const {once} = require("node:events");
const {chromium, expect} = require("playwright/test");

(async () => {
  const repo = path.resolve(__dirname, "..");
  fs.mkdirSync(path.join(repo, "tmp"), {recursive: true});
  const root = fs.mkdtempSync(path.join(repo, "tmp", "browser-smoke-"));
  const backend = spawn(process.env.PYTHON || "python", ["-u", path.join(__dirname, "browser_runtime_fixture.py"), root], {cwd: repo});
  backend.stderr.pipe(fs.createWriteStream(path.join(root, "server.log")));
  const exited = once(backend, "exit");
  let browser;
  let page;
  const checks = [];
  const errors = [];
  async function check(name, fn) { await fn(); checks.push(name); console.log("PASS " + name); }
  try {
    const lines = readline.createInterface({input: backend.stdout});
    const [line] = await Promise.race([once(lines, "line"), exited.then(() => {throw Error("backend failed to start; see " + root);})]);
    const {url} = JSON.parse(line);
    browser = await chromium.launch({channel: process.env.BROWSER_CHANNEL || "chrome", headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    page = await context.newPage();
    page.on("pageerror", e => errors.push(String(e)));
    page.on("request", r => {if(r.method()==="POST") fs.appendFileSync(path.join(root,"requests.jsonl"),JSON.stringify({url:r.url(),body:r.postDataJSON()})+"\n");});
    const click = name => page.getByRole("button", {name, exact: true}).click();
    const snap = name => page.screenshot({path: path.join(root, name + ".png"), fullPage: true, animations: "disabled"});
    const data = async name => (await page.request.get(url + "/api/" + name)).json();
    await page.goto(url + "/#/schemas");
    await check("empty real registry has no mock Schemas", async () => {
      await expect(page.getByRole("heading", {name: "还没有保存的模板", exact:true})).toBeVisible();
      assert.equal((await data("schemas")).items.length, 0);
      await expect(page.locator("body")).not.toContainText("sha256:mock-schema-");
    });
    await check("desktop sidebar collapse persists across reload", async () => {
      await expect(page.locator(".workspace-tab")).toHaveCount(2);
      await expect(page.locator("#nav-templates")).toHaveAttribute("aria-current", "page");
      await page.getByRole("button", {name: "收起侧栏", exact: true}).click();
      await expect.poll(() => page.locator("#appSidebar").evaluate(node => node.getBoundingClientRect().width)).toBeLessThanOrEqual(80);
      await expect(page.getByRole("link", {name: "实验研究", exact: true})).toBeVisible();
      await snap("sidebar-collapsed");
      await page.reload();
      await expect(page.locator("#mobileNavToggle")).toHaveAttribute("aria-expanded", "false");
      await page.getByRole("button", {name: "展开侧栏", exact: true}).click();
      await expect.poll(() => page.locator("#appSidebar").evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThan(200);
    });
    await page.getByRole("link", {name: "模型文件", exact: true}).click();
    await page.getByRole("textbox", {name:"模型文件内容"}).fill('# browser smoke\nset x = 0.5\nextract name="generic_figure_of_merit" max(x)\n');
    await click("解析模型文件");
    await page.getByRole("textbox", {name:"pkg-solar-cell-tcad", exact:true}).fill("browser-smoke-package");
    await click("保存模型文件");
    await check("package upload and registration", async () => {
      await expect(page.getByRole("button", {name:"配置模板", exact:true})).toBeVisible();
      assert.equal((await data("packages")).items.length, 1);
    });
    await snap("01-package");
    await click("配置模板");
    await page.locator("#template-name").fill("厚度优化模板");
    await check("template save failure retains draft and retries the same identity", async () => {
      await page.route("**/api/problems", route => route.request().method() === "POST"
        ? route.fulfill({status:503,contentType:"application/json",body:JSON.stringify({error:"temporary test failure sha256:"+"a".repeat(64)})}) : route.continue());
      await click("保存模板");
      await expect(page.locator(".submit-msg.err")).toContainText("模板尚未保存完整");
      assert.equal((await data("schemas")).items.length,1);
      assert.equal((await data("problems")).items.length,0);
      await expect(page.locator("#template-name")).toHaveValue("厚度优化模板");
      assert.ok(!(await page.locator("body").innerText()).includes("temporary test failure"));
      await page.unroute("**/api/problems");
    });
    await check("sidebar and polling preserve the template draft", async () => {
      await page.locator("#mobileNavToggle").click();
      await expect(page.locator("#template-name")).toHaveValue("厚度优化模板");
      await page.locator("#mobileNavToggle").click();
      await page.getByRole("combobox", {name:"自动刷新间隔"}).selectOption({label:"5s"});
      await page.waitForTimeout(5600);
      await expect(page.locator("#template-name")).toHaveValue("厚度优化模板");
    });
    await click("保存模板");
    await expect(page.locator(".schema-registered-title")).toContainText("模板已保存");
    const schemas = (await data("schemas")).items;
    const schemaRev = schemas[0].revision;
    const problemId = (await data("problems")).items[0].problem_id;
    await check("one save creates a complete template without duplicate versions", async () => {
      assert.equal(schemas.length, 1);
      assert.equal((await data("problems")).items.length, 1);
      assert.equal(schemas[0].problem_hint,"厚度优化模板");
      assert.ok(!(await page.locator("body").innerText()).includes("sha256:"));
      await expect(page.locator("#template-solver")).toHaveValue("minimal-simulation");
      await snap("template-saved");
    });
    await page.locator(".schema-registered-banner").getByRole("button", {name:"⚡ 创建研究",exact:true}).click();
    await page.locator("#s-study").fill("第一轮厚度对比");
    await click("创建研究");
    await expect(page.locator(".submit-msg.ok")).toContainText("研究已创建");
    const studyId = (await data("studies")).items[0].study_id;
    await click("开始第一次运行");
    await check("candidate inherits actual Problem Schema", async () => {
      await expect(page.locator(".workspace-tab")).toHaveCount(2);
      await expect(page.locator("#nav-compose")).toHaveAttribute("aria-current", "page");
      await snap("research-workspace");
      await expect(page.locator("#c-schema-rev")).toHaveValue(schemaRev);
      await expect(page.getByRole("spinbutton")).toHaveValue("0.5");
      await expect(page.getByRole("checkbox",{name:"generic_figure_of_merit"})).toBeChecked();
      await expect(page.locator("body")).not.toContainText("t_total1 float");
    });
    await check("out-of-range parameter cannot submit", async () => {
      await page.getByRole("spinbutton").fill("999");
      await click("检查参数");
      await expect(page.locator(".preflight-box")).toContainText("超出范围");
      await expect(page.getByRole("button",{name:"检查运行配置",exact:true})).toBeDisabled();
      assert.equal((await data("evaluations")).items.length,0);
    });
    await page.getByRole("spinbutton").fill("0.75");
    await click("检查参数");
    await click("检查运行配置");
    await expect(page.getByRole("button", {name:"提交运行",exact:true})).toBeEnabled();
    await check("editing requested outputs invalidates preview", async()=> {
      await page.getByRole("checkbox",{name:"generic_figure_of_merit"}).uncheck();
      await expect(page.getByRole("button",{name:"提交运行",exact:true})).toBeDisabled();
      await page.getByRole("checkbox",{name:"generic_figure_of_merit"}).check();
      await click("检查运行配置");
    });
    await click("提交运行");
    await expect(page.locator("body")).toContainText("已提交，等待运行。");
    await check("queued evaluation is persisted once", async () => {
      const rows=(await data("evaluations")).items;
      assert.equal(rows.length,1); assert.equal(rows[0].status,"queued");
      await click("提交运行");
      assert.equal((await data("evaluations")).items.length,1);
    });
    fs.writeFileSync(path.join(root,"runtime.enabled"),"");
    await click("查看进度与结果");
    await check("runtime qualifies and monitoring refreshes", async () => {
      await expect(page.getByRole("cell",{name:"已通过",exact:true})).toBeVisible({timeout:20000});
      await click("1 次执行 ▼");
      await expect(page.locator("body")).toContainText("已完成");
    });
    await snap("02-qualified");
    await page.reload();
    await expect(page.getByRole("cell",{name:"已通过",exact:true})).toBeVisible();
    checks.push("result survives browser reload");
    for(const name of ["总览","计算资源","运行性能","算法运行"]) {
      await page.getByRole("link",{name,exact:true}).click();
      await expect(page.getByRole("heading",{name,exact:true})).toBeVisible();
      if(name==="计算资源") await expect(page.locator("main")).not.toContainText("停用");
      if(name==="运行性能") await expect(page.locator("main")).not.toContainText("∞");
      await snap("monitor-"+name);
      checks.push("monitor page: "+name);
    }
    await check("offline cache is marked stale and recovers", async()=> {
      await page.getByRole("link",{name:"总览",exact:true}).click();
      await expect(page.getByRole("link",{name:"第一轮厚度对比",exact:true})).toBeVisible();
      await page.getByRole("combobox",{name:"自动刷新间隔"}).selectOption({label:"5s"});
      await context.setOffline(true);
      try {
        await expect(page.locator("#banner")).toHaveClass(/show/,{timeout:12000});
        await expect(page.locator("#banner")).toContainText("服务不可达");
        await expect(page.getByRole("link",{name:"第一轮厚度对比",exact:true})).toBeVisible();
        await snap("offline");
      } finally {await context.setOffline(false);}
      await expect(page.locator("#banner")).not.toHaveClass(/show/,{timeout:12000});
      await expect(page.locator("#healthtxt")).toHaveText("服务正常");
    });
    await check("editing a template preserves old studies and new studies use the selected version", async () => {
      await page.goto(url+"/#/schemas");
      await page.locator(".schemas-catalog-section").getByRole("button",{name:"编辑",exact:true}).first().click();
      await click("编辑原始 JSON");
      const source = JSON.parse(await page.locator("#schema-json-textarea").inputValue());
      source.problem_hint = "厚度优化模板第二版";
      source.parameters[0].default = 0.8;
      await page.locator("#schema-json-textarea").fill(JSON.stringify(source,null,2));
      await click("保存模板");
      await expect(page.locator(".schema-registered-title")).toContainText("模板已保存");
      await click("表单模式");
      const versions = (await data("problems")).items;
      assert.equal(versions.length,2);
      assert.equal(versions[1].problem_id,problemId);
      await page.locator(".schema-registered-banner").getByRole("button",{name:"⚡ 创建研究",exact:true}).click();
      await expect(page.locator("#s-problem-select")).toHaveValue(JSON.stringify([problemId,versions[1].revision]));
      await page.locator("#s-study").fill("第二轮厚度对比");
      await click("创建研究");
      await expect(page.locator(".submit-msg.ok")).toContainText("研究已创建");
      await click("开始第一次运行");
      await expect(page.getByRole("spinbutton")).toHaveValue("0.8");
      await page.locator("#target-study-select-select").selectOption(studyId);
      await expect(page.getByRole("spinbutton")).toHaveValue("0.5");
      await click("检查运行配置");
      await expect(page.locator("#btn-confirm-eval")).toBeEnabled();
      assert.ok(!(await page.locator("body").innerText()).includes(studyId));
      await expect(page.locator("#c-schema-rev")).toHaveValue(schemaRev);
    });
    await check("technical versions are opt-in on all workspaces", async () => {
      for(const route of ["/", "/compose?step=1", "/compose?step=2", "/compose?step=3", "/compose?step=4", "/submit", "/capacity", "/shapes", "/algorithms", "/study/"+encodeURIComponent(studyId), "/problem/"+encodeURIComponent(problemId)]) {
        await page.goto(url+"/#"+route);
        await expect(page.locator("#healthtxt")).toHaveText("服务正常");
        await expect.poll(async () => await page.locator("main").innerText()).not.toContain("正在加载");
        await page.waitForTimeout(250);
        const visible = await page.locator("body").innerText();
        assert.ok(!/sha256:|[a-f0-9]{32,}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}/i.test(visible), route+" displays a technical hash: "+visible);
        for (const input of await page.locator("input,textarea,select").all()) {
          if(await input.isVisible()) assert.ok(!/sha256:|[a-f0-9]{32,}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}/i.test((await input.evaluate(node => node.tagName === "SELECT" ? node.selectedOptions[0]?.textContent || "" : node.value))),route+" displays an encoded field");
        }
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,route+" overflow");
      }
      await page.goto(url+"/#/packages");
      const version = page.locator(".technical-value").first();
      await version.locator("summary").click();
      await expect(version.locator("code")).toContainText("sha256:");
      await page.keyboard.press("Tab");
      await expect(version.getByRole("button", {name:"复制",exact:true})).toBeFocused();
    });
    await check("English and Chinese language switching", async()=> {
      await click("切换语言");
      await expect(page.locator("html")).toHaveAttribute("lang","en");
      await page.getByRole("button",{name:/Switch language|切换语言/}).click();
      await expect(page.locator("html")).toHaveAttribute("lang","zh-CN");
    });
    await page.goto(url+"/#/study/"+encodeURIComponent(studyId));
    await expect(page.getByRole("cell",{name:"已通过",exact:true})).toBeVisible();
    await page.setViewportSize({width:390,height:844});
    await check("mobile drawer opens and closes without covering content", async()=> {
      const right = () => page.locator("#appSidebar").evaluate(el=>el.getBoundingClientRect().right);
      await expect.poll(right).toBeLessThanOrEqual(0);
      await page.locator("#mobileNavToggle").click();
      await expect(page.locator("#appSidebar")).toHaveClass(/open/);
      await expect(page.locator(".app-main-canvas")).toHaveJSProperty("inert", true);
      await expect(page.locator("#sidebarClose")).toBeFocused();
      await snap("sidebar-mobile-open");
      await page.keyboard.press("Escape");
      await expect(page.locator("#mobileNavToggle")).toBeFocused();
      await expect(page.locator("#appSidebar")).toHaveJSProperty("inert", true);
      await expect(page.locator(".app-main-canvas")).toHaveJSProperty("inert", false);
      await expect.poll(right).toBeLessThanOrEqual(0);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    });
    await snap("03-mobile");
    assert.deepEqual(errors,[]);
    console.log("Artifacts: "+root);
  } finally {
    if(page) { try {await page.screenshot({path:path.join(root,"final.png"),fullPage:true});} catch{} }
    if(browser) await browser.close();
    fs.writeFileSync(path.join(root,"server.stop"),"");
    await exited;
    fs.writeFileSync(path.join(root,"browser-report.json"),JSON.stringify({checks,errors},null,2));
    console.log("Evidence directory: "+root);
  }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
