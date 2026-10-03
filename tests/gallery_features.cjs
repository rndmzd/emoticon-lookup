// Run our generated script in jsdom. No browser, layout engine, or media playback is involved.
const fs=require('fs'),assert=require('assert/strict');
const path=require('path');
const {JSDOM,VirtualConsole}=require('jsdom');
const fixturePath=path.join(__dirname,'fixtures','gallery.html');
if(!fs.existsSync(fixturePath)){
  const result=require('child_process').spawnSync(process.env.PYTHON||'python',['scripts/build_examples.py','--fixtures'],{cwd:path.join(__dirname,'..'),stdio:'inherit'});
  if(result.status!==0)throw new Error('Could not generate synthetic test fixtures. Set PYTHON to your Python executable.');
}
const fixture=fs.readFileSync(fixturePath,'utf8');
const storageKey='emoticon-lookup:groups:v1:'+fixture.match(/data-gallery-key="([^"]+)"/)[1];
function load(html=fixture,{stored,blocked=false}={}){
  const errors=[],downloads=[],console=new VirtualConsole();console.on('jsdomError',error=>errors.push(error));
  const dom=new JSDOM(html,{url:'https://gallery.test/',runScripts:'dangerously',virtualConsole:console,beforeParse(w){
    w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
    w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
    w.Element.prototype.scrollTo=function(){};w.Blob=Blob;
    w.URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:test'};w.URL.revokeObjectURL=()=>{};
    w.HTMLAnchorElement.prototype.click=function(){};
    Object.defineProperty(w.navigator,'clipboard',{value:{async writeText(text){w.copied=text}}});
    if(stored)w.localStorage.setItem(storageKey,JSON.stringify(stored));
    if(blocked)Object.defineProperty(w,'localStorage',{get(){throw new Error('Storage blocked')}});
  }});
  assert.deepEqual(errors,[],'Generated script must run without errors');
  const w=dom.window,d=w.document;return {dom,w,d,$:id=>d.getElementById(id),errors,downloads};
}
function change(c,id,value,type='change'){c.$(id).value=value;c.$(id).dispatchEvent(new c.w.Event(type,{bubbles:true}))}
function names(c){return [...c.$('grid').querySelectorAll('.card')].map(card=>card.dataset.name)}
function save(c,form,name,rules,sensitive=false){form.querySelector('input[type=text]').value=name;form.querySelector('textarea').value=rules;form.querySelector('input[type=checkbox]').checked=sensitive;form.dispatchEvent(new c.w.Event('submit',{bubbles:true,cancelable:true}))}
function create(c,name,rules,sensitive=false){c.$('add-group').click();const form=[...c.$('group-list').querySelectorAll('form')].at(-1);save(c,form,name,rules,sensitive);return form.dataset.groupId}
function snapshot(c){return JSON.parse(c.w.localStorage.getItem(storageKey))}
async function importFile(c,value){const input=c.$('group-file');Object.defineProperty(input,'files',{configurable:true,value:[{size:100,text:async()=>JSON.stringify(value)}]});input.dispatchEvent(new c.w.Event('change',{bubbles:true}));await new Promise(resolve=>setTimeout(resolve,0))}
(async()=>{
  let checks=0;
  {
    const c=load();assert.equal(c.$('result-count').textContent,'3 of 3 names');
    change(c,'search',':DEMO','input');assert.deepEqual(names(c),['demo_animation']);
    change(c,'search','no-such-name','input');assert.ok(c.$('empty').classList.contains('show'));
    change(c,'search','','input');change(c,'filter','unresolved');assert.deepEqual(names(c),['etextrary']);
    change(c,'filter','ok');assert.equal(names(c).length,2);
    change(c,'filter','all');change(c,'sort','frequency');assert.equal(names(c)[0],'etextraryan');
    const originals=[...c.$('grid').querySelectorAll('.visual img')].map(img=>img.src);
    c.$('animate').checked=false;c.$('animate').dispatchEvent(new c.w.Event('change'));
    assert.ok([...c.$('grid').querySelectorAll('.visual img')].every(img=>img.src===img.dataset.poster));
    c.$('animate').checked=true;c.$('animate').dispatchEvent(new c.w.Event('change'));
    assert.deepEqual([...c.$('grid').querySelectorAll('.visual img')].map(img=>img.src),originals);
    c.$('grid').querySelector('.copy').click();await Promise.resolve();assert.equal(c.w.copied,':etextraryan');
    assert.deepEqual(c.errors,[]);c.w.close();checks++;
  }
  {
    const c=load(),image=c.$('grid').querySelector('[data-name=demo_animation] .visual img'),original=image.src;
    c.$('animate').checked=false;c.$('animate').dispatchEvent(new c.w.Event('change'));
    image.click();assert.equal(c.$('media-viewer').open,true);assert.equal(c.$('viewer-image').src,original);
    assert.equal(c.$('viewer-name').textContent,':demo_animation');assert.equal(c.d.body.style.overflow,'hidden');
    assert.ok(!c.$('media-viewer').classList.contains('fit'));assert.ok(c.$('viewer-size').textContent.includes('100%'));
    Object.defineProperties(c.$('viewer-image'),{naturalWidth:{value:240},naturalHeight:{value:120}});
    c.$('viewer-image').dispatchEvent(new c.w.Event('load'));assert.ok(c.$('viewer-size').textContent.includes('240 × 120'));
    c.$('viewer-fit').click();assert.ok(c.$('media-viewer').classList.contains('fit'));
    c.$('viewer-fit').click();assert.ok(!c.$('media-viewer').classList.contains('fit'));
    c.$('viewer-close').click();assert.equal(c.$('media-viewer').open,false);assert.ok(!c.$('viewer-image').hasAttribute('src'));assert.equal(c.d.body.style.overflow,'');
    image.click();c.$('media-viewer').click();assert.equal(c.$('media-viewer').open,false);
    assert.match(fixture,/\.viewer-stage img\{[^}]*max-width:none;max-height:none/);assert.match(fixture,/\.viewer::backdrop\{background:rgba\(/);
    c.w.close();checks++;
  }
  {
    const c=load(),first=create(c,'First','^etextrary$\nDEMO');assert.deepEqual(new Set(names(c)),new Set(['etextrary','demo_animation']));
    const second=create(c,'Second','etextra\ndemo');assert.equal(names(c).length,3);
    change(c,'group-view','grouped');assert.equal(c.$('grid').querySelectorAll('.group-section').length,2);assert.equal(names(c).length,5);assert.ok(c.$('result-count').textContent.startsWith('3 of 3'));
    const duplicate=c.$('grid').querySelectorAll('[data-name=demo_animation]')[1];duplicate.querySelector('.preview-open').click();assert.equal(c.$('viewer-name').textContent,':demo_animation');c.$('viewer-close').click();
    duplicate.querySelector('.copy').click();await Promise.resolve();assert.equal(c.w.copied,':demo_animation');
    change(c,'group-view','ungrouped');assert.deepEqual(names(c),[]);
    save(c,c.$('group-list').querySelector('form'),'First','DEMO',true);change(c,'group-view','group:'+first);assert.deepEqual(names(c),[]);
    save(c,c.$('group-list').querySelector('form'),'First','DEMO',false);assert.deepEqual(names(c),['demo_animation']);
    const stored=snapshot(c);assert.equal(stored.groups.length,2);assert.equal(stored.groups.find(group=>group.id===second).rules[0],'etextra');
    const reopened=load(fixture,{stored});assert.deepEqual(names(reopened),['demo_animation']);reopened.w.close();c.w.close();checks++;
  }
  {
    const c=load();create(c,'Valid','demo');const before=snapshot(c).groups;
    const form=c.$('group-list').querySelector('form');save(c,form,'Bad','[');assert.ok(form.querySelector('.group-error').textContent.includes('Could not save'));assert.deepEqual(snapshot(c).groups,before);assert.deepEqual(names(c),['demo_animation']);
    await importFile(c,{version:1,groups:[{name:'Invalid',rules:['(']}]});assert.ok(c.$('import-error').textContent.includes('Could not import'));assert.deepEqual(snapshot(c).groups,before);
    await importFile(c,{version:1,groups:[{id:before[0].id,name:'Imported',rules:['^et'],caseSensitive:false}]});assert.equal(snapshot(c).groups.length,2);assert.notEqual(snapshot(c).groups[0].id,snapshot(c).groups[1].id);assert.equal(c.$('group-view').value,'grouped');
    change(c,'group-view','group:'+before[0].id);c.$('group-list').querySelector('form button.secondary').click();assert.equal(snapshot(c).groups.length,1);assert.equal(c.$('group-view').value,'all');
    c.$('export-groups').click();assert.equal(JSON.parse(await c.downloads.at(-1).text()).groups[0].name,'Imported');c.w.close();checks++;
  }
  {
    const c=load();create(c,'</script><img src=x onerror="window.injected=true">','demo');create(c,'Overlap','demo|et');change(c,'group-view','grouped');assert.equal(names(c).length,4);
    c.$('animate').checked=false;c.$('animate').dispatchEvent(new c.w.Event('change'));c.$('save-gallery').click();const portable=await c.downloads.at(-1).text();assert.ok(portable.includes('\\u003c/script>'));assert.ok(!portable.includes('src="x" onerror='));
    const reopened=load(portable,{blocked:true});assert.equal(reopened.$('group-list').querySelectorAll('form').length,2);assert.equal(names(reopened).length,4);assert.equal(reopened.w.injected,undefined);assert.equal(reopened.d.querySelectorAll('script').length,2);
    const inert=new JSDOM(portable);assert.equal(inert.window.document.querySelectorAll('.card').length,3);assert.ok(inert.window.document.querySelector('[data-name=demo_animation] img').src.startsWith('data:image/gif;'));
    inert.window.close();reopened.w.close();c.w.close();checks++;
  }
  {
    const c=load(),first=create(c,'First','demo'),second=create(c,'Second','et');
    const other=[...c.$('group-list').querySelectorAll('form')].find(form=>form.dataset.groupId===second);
    other.querySelector('textarea').value='^etextrary$';other.querySelector('textarea').dispatchEvent(new c.w.Event('input'));
    save(c,c.$('group-list').querySelector('form'),'First updated','demo');
    const preserved=[...c.$('group-list').querySelectorAll('form')].find(form=>form.dataset.groupId===second);
    assert.equal(preserved.querySelector('textarea').value,'^etextrary$');assert.ok(preserved.textContent.includes('Unsaved changes'));assert.equal(snapshot(c).groups.find(group=>group.id===second).rules[0],'et');
    c.w.close();checks++;
  }
  {
    const c=load(fixture,{blocked:true});create(c,'Offline','demo');assert.ok(c.$('group-storage').textContent.includes('storage is unavailable'));assert.deepEqual(names(c),['demo_animation']);
    c.$('save-gallery').click();const portable=await c.downloads.at(-1).text(),reopened=load(portable,{blocked:true});assert.deepEqual(names(reopened),['demo_animation']);reopened.w.close();c.w.close();checks++;
  }
  console.log('PASS: '+checks+' DOM scenarios cover controls, full-size overlay, regex OR/overlap/case sensitivity, invalid rules, persistence, import/export, safe portable copies, and unavailable storage. Browser layout/playback remains unverified.');
})().catch(error=>{console.error(error);process.exitCode=1});
