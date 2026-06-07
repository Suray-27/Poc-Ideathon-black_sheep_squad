import os
import sqlite3
import pandas as pd
from crewai import Agent, Task, Crew, Process
from crewai.tools import tool

# Initialize Gemini (Using 1.5 Pro for stable structured reasoning and SQL)
gemini_llm = "gemini/gemini-2.5-flash"
# -------------------------------------------------------------
# STEP 1: Define Custom Database Tools for the Agents
# -------------------------------------------------------------
@tool("Fetch Database Schema")
def fetch_schema(db_name: str = 'source_warehouse.db') -> str:
    """Connects to the specified SQLite database and returns the schema layout of all tables."""
    try:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        conn.close()
        
        schema_info = "Database Schema Layout:\n"
        for table_name, sql in tables:
            schema_info += f"\nTable: {table_name}\nDDL:\n{sql}\n"
        return schema_info
    except Exception as e:
        return f"Error fetching schema: {str(e)}"

# -------------------------------------------------------------
# STEP 2: Define the PipelineIQ Agent Crew
# -------------------------------------------------------------

# 1. The Data Architect Agent
architect = Agent(
    role='Data Architect',
    goal='Analyze user data requests, inspect available source schemas, and design the correct target data structure.',
    backstory='You are an expert data architect. You understand relational modeling, join keys, and how to structure clean analytics-ready data structures based on source metadata.',
    tools=[fetch_schema],
    llm=gemini_llm,
    verbose=True
)

# 2. The Data Engineer Agent
engineer = Agent(
    role='Data Engineer',
    goal='Write clean, highly accurate ANSI SQL queries to transform raw source data based on the Architect\'s plan.',
    backstory='You are a master SQL developer. You write optimized queries, handle explicit joins cleanly, and return ONLY the raw code blocks needed for execution.',
    llm=gemini_llm,
    verbose=True
)

# -------------------------------------------------------------
# STEP 3: Define the Sequential Workflow Tasks
# -------------------------------------------------------------

user_prompt = "I need a report showing the total amount spent by each user, including their name and email, sorted by total spent descending."

task_analyze_schema = Task(
    description=(
        f"1. Read the user request: '{user_prompt}'\n"
        "2. Use the Fetch Database Schema tool to investigate the tables available in 'source_warehouse.db'.\n"
        "3. Define exactly which tables, columns, and join conditions are required to satisfy the request."
    ),
    expected_output="A structured logical data blueprint specifying the exact source tables, mapping columns, and target schema layout.",
    agent=architect
)

task_generate_sql = Task(
    description=(
        "Using the blueprint provided by the Data Architect, write a valid SQL query to pull the requested data.\n"
        "CRITICAL: Return ONLY the raw executable SQL query string. Do not wrap it in markdown code blocks like ```sql."
    ),
    expected_output="A single, raw executable SQL query string.",
    agent=engineer
)

# Assemble the Crew
pipeline_crew = Crew(
    agents=[architect, engineer],
    tasks=[task_analyze_schema, task_generate_sql],
    process=Process.sequential
)

# -------------------------------------------------------------
# STEP 4: Kick off the Crew & Execute the Generated Pipeline
# -------------------------------------------------------------
if __name__ == "__main__":
    print(f"🚀 Business Team Request: '{user_prompt}'\n")
    print("🤖 Deploying PipelineIQ Crew powered by Gemini...")
    
    # Run the agents
    result = pipeline_crew.kickoff()
    
    # Extract the raw SQL string from the response
    generated_sql = str(result).strip()
    
    # Quick cleanup in case the model ignored formatting rules and injected markdown wrappers
    # Safety sanitization fallback check to handle markdown blocks safely
    if "```" in generated_sql:
        # Split by the code block marker and grab the actual code inside
        parts = generated_sql.split("```")
        for part in parts:
            if "SELECT" in part.upper():
                generated_sql = part
                break
        
        # Clean out any leftover "sql" language specifier tags
        if generated_sql.lower().startswith("sql"):
            generated_sql = generated_sql[3:]
            
    generated_sql = generated_sql.strip()
    
    print("\n" + "="*50)
    print("🛠️ GENERATED PIPELINE CODE:")
    print("="*50)
    print(generated_sql)
    print("="*50 + "\n")
    
    # Execute the query against our source DB and output the final delivery
    print("💾 Executing pipeline and delivering analysis-ready data...")
    try:
        conn = sqlite3.connect('source_warehouse.db')
        df = pd.read_sql_query(generated_sql, conn)
        conn.close()
        
        print("\n🏆 TARGET ANALYSIS-READY OUTPUT DATA:")
        print(df.to_markdown(index=False))
        
        # Save output mimicking ingestion delivery
        df.to_csv('pipeline_output.csv', index=False)
        print("\n💾 Data delivered successfully to 'pipeline_output.csv'")
        
    except Exception as e:
        print(f"\n❌ Pipeline Execution Failed: {str(e)}")