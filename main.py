import os
import traceback
from flask import Flask
from crewai import Agent, Task, Crew, LLM

app = Flask(__name__)

# Updated Gemini 2.5 Flash Model
gemini_llm = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=os.environ.get("GEMINI_API_KEY")
)

dispatcher = Agent(
    role="Truck Dispatch Manager",
    goal="Analyze truck owner leads and prepare outreach emails",
    backstory="Expert US truck dispatcher helping owner-operators find high-paying freight loads.",
    llm=gemini_llm,
    verbose=False
)

@app.route('/')
def home():
    return "CrewAI Truck Dispatch Agent is Active and Running!", 200

@app.route('/run-agent')
def run_agent():
    try:
        task = Task(
            description="Write a short, professional, spam-free cold email to a box truck / semi owner offering 5% dispatch service.",
            expected_output="Plain text email body with a catchy subject line, no placeholders.",
            agent=dispatcher
        )
        crew = Crew(agents=[dispatcher], tasks=[task])
        result = crew.kickoff()
        return f"<pre>{str(result)}</pre>", 200
    except Exception as e:
        return f"<pre>Error:\n{traceback.format_exc()}</pre>", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
