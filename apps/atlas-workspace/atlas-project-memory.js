
(() => {
 const el=id=>document.getElementById(id);
 const box=document.createElement("section");
 box.className="card";
 box.setAttribute("aria-label","Persistent projects and memory");
 box.innerHTML=`
 <h3>Persistent projects and memory</h3>
 <p>Single-user storage on this computer. Not yet supplied to the model.
 Do not save passwords or account credentials.</p>
 <p id="pmStatus" role="status" aria-live="polite">Loading saved records…</p>
 <label for="pmName">New project name</label>
 <input id="pmName" maxlength="200">
 <div class="actions"><button id="pmCreate">Create project</button></div>
 <label for="pmProject">Save memory under</label>
 <select id="pmProject"></select>
 <label for="pmText">Preference, fact, or project decision</label>
 <textarea id="pmText" maxlength="20000"></textarea>
 <div class="actions">
 <button id="pmSave">Save memory</button>
 <button id="pmReload">Reload records</button>
 <button id="pmDictate">Dictate into selected field</button>
 </div>
 <h3>Saved memory</h3><div id="pmRecords"></div>`;
 el("main").insertBefore(box,el("editor"));
 let busy=false, target=el("pmText"), recognition=null;
 const say=text=>el("pmStatus").textContent=text;
 [el("pmName"),el("pmText")].forEach(field=>{
   field.addEventListener("focus",()=>target=field);
 });

 function visibility(){
   box.hidden=!["Projects","Memory"].includes(el("title").textContent);
 }
 new MutationObserver(visibility).observe(el("title"),{
   childList:true,characterData:true,subtree:true
 });
 visibility();

 async function api(path,data){
   const response=await fetch("/api/workspace/"+path,{
     method:data===undefined?"GET":"POST",
     headers:{"Content-Type":"application/json","X-Atlas-Workspace":"1"},
     ...(data===undefined?{}:{body:JSON.stringify(data)})
   });
   const result=await response.json();
   if(!response.ok)throw Error(result.detail||"Workspace request failed.");
   return result;
 }

 async function refresh(){
   const selected=el("pmProject").value;
   const projects=await api("projects");
   el("pmProject").replaceChildren(new Option("Personal preferences",""));
   for(const project of projects.items)
     el("pmProject").add(new Option(project.name,project.id));
   el("pmProject").value=selected;
   const memories=await api("memories");
   el("pmRecords").replaceChildren();
   for(const memory of memories.items){
     const article=document.createElement("article");
     article.style.cssText="border-bottom:1px solid #43516b;padding:14px 0";
     const heading=document.createElement("h4");
     heading.textContent=memory.project_name||"Personal preferences";
     const text=document.createElement("p");
     text.style.whiteSpace="pre-wrap";text.textContent=memory.content;
     const remove=document.createElement("button");
     remove.textContent="Forget this memory";
     remove.setAttribute("aria-label","Forget memory: "+memory.content.slice(0,100));
     remove.onclick=()=>run(async()=>{
       if(!confirm("Delete this memory?"))return;
       await api("forget",{id:memory.id});
       await refresh();say("Memory deleted.");
     });
     article.append(heading,text,remove);el("pmRecords").append(article);
   }
   say(projects.items.length+" projects and "+memories.items.length+" memories loaded.");
 }

 async function run(work){
   if(busy)return;
   busy=true;box.setAttribute("aria-busy","true");
   box.querySelectorAll("button").forEach(button=>button.disabled=true);
   try{await work()}catch(error){say(error.message)}
   finally{
     busy=false;box.removeAttribute("aria-busy");
     box.querySelectorAll("button").forEach(button=>button.disabled=false);
   }
 }

 el("pmCreate").onclick=()=>run(async()=>{
   const name=el("pmName").value.trim();
   if(!name)throw Error("Project name is empty.");
   const project=await api("projects",{name});
   el("pmName").value="";await refresh();
   el("pmProject").value=project.id;say("Project created and selected.");
 });
 el("pmSave").onclick=()=>run(async()=>{
   const content=el("pmText").value.trim();
   if(!content)throw Error("Memory is empty.");
   await api("memories",{project_id:el("pmProject").value||null,content});
   el("pmText").value="";await refresh();say("Memory saved.");
 });
 el("pmReload").onclick=()=>run(refresh);
 el("pmDictate").onclick=()=>{
   const Speech=window.SpeechRecognition||window.webkitSpeechRecognition;
   if(!Speech){say("Browser dictation unavailable. Existing device dictation can be used.");return}
   if(recognition){recognition.stop();return}
   if(!confirm("Browser dictation may send microphone audio to the browser provider. Continue?"))return;
   const destination=target;
   const session=new Speech();
   recognition=session;session.lang="en-US";
   session.onresult=event=>{
     const text=event.results[0][0].transcript;
     destination.value+=(destination.value?" ":"")+text;
     say("Dictation added. Activate Create project or Save memory to save it.");
   };
   session.onerror=event=>say("Dictation error: "+event.error);
   session.onend=()=>{recognition=null;el("pmDictate").textContent="Dictate into selected field"};
   try{
     session.start();el("pmDictate").textContent="Stop dictation";
     say("Listening for "+(destination.id==="pmName"?"project name":"memory")+".");
   }catch(error){recognition=null;say("Could not start dictation.")}
 };
 run(refresh);
})();
