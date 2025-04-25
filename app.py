import pandas as pd
import dash
import dash_bootstrap_components as dbc
from dash import html
from dash.dependencies import Input, Output, State, MATCH
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

#local testing only
# from dotenv import load_dotenv
# load_dotenv()

#load and clean data

#authenticate and load data from Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.getenv("GOOGLE_CREDS")
if not creds_json:
    raise ValueError("Missing GOOGLE_CREDS environment variable")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
file_name = os.getenv('file_name')

def load_data(client):
    def get_timesheets(client):
        sheet = client.open(file_name).sheet1
        data = sheet.get_all_records()
        df = pd.DataFrame(data[1:], columns=data[0])  # first row as column names
        df=df[['username','fname','local_date','local_end_time','hours','jobcode_1','jobcode_2']]

        # Create billing code column
        df['billing_code'] = df.apply(  
            lambda row: row['jobcode_1'] if pd.isna(row['jobcode_2']) or row['jobcode_2'] == '' 
            else row['jobcode_1'] + ' > ' + row['jobcode_2'], axis=1)
        
        return df
    
    def get_alloc(client):
        sheet = client.open('weekly_time_data').get_worksheet(1)
        data = sheet.get_all_records()
        df = pd.DataFrame(data)
        df.columns = ['recruiter','recruiter_hours', 'trs','trs_hours','acct_owner','client','job','billing_code']

        # restructure alloc data as long list, separating recruiter and trs
        no_trs = df[df['trs'].isna()][['recruiter', 'recruiter_hours', 'client', 'job', 'billing_code']]
        with_trs = df[~df['trs'].isna()]
        with_trs_expanded = pd.DataFrame({
            'recruiter': with_trs['recruiter'].tolist() + with_trs['trs'].tolist(),
            'recruiter_hours': with_trs['recruiter_hours'].tolist() + with_trs['trs_hours'].tolist(),
            'client': with_trs['client'].tolist() * 2,
            'job': with_trs['job'].tolist() * 2,
            'billing_code': with_trs['billing_code'].tolist() * 2
            })
        df_long = pd.concat([no_trs, with_trs_expanded], axis=0).sort_values(by='job', ignore_index=True)
        df_long.replace('', pd.NA, inplace=True)  # Replace empty strings with NaN
        df_long.dropna(subset=['recruiter', 'recruiter_hours'], inplace=True)
        df_long['recruiter'] = df_long['recruiter'].replace('Madelyn', 'Maddie')

        return df_long
    
    #get hours data
    timesheets = get_timesheets(client)
    timesheets.replace('Madelyn', 'Maddie', inplace=True)
    hours_sum = timesheets.groupby(['fname','jobcode_1','jobcode_2', 'billing_code']).agg({'hours':'sum'}).reset_index()
    #get update date
    timesheets['local_end_time'] = pd.to_datetime(timesheets['local_end_time'], errors='coerce')
    update_date = timesheets['local_end_time'].max()

    #get allocation data
    allocation = get_alloc(client)

    #merge alloc and hours data
    combined_table = pd.merge(hours_sum, allocation,
        how='outer',
        left_on = ['billing_code', 'fname'],
        right_on = ['billing_code', 'recruiter'],
    )[['fname', 'recruiter', 'billing_code', 'hours', 'recruiter_hours', 'client', 'job']]
    combined_table.rename(columns={'hours': 'hours_actual','recruiter_hours': 'hours_allocated'}, inplace=True)

    #combined table cleanup

    #match names from both source tables, then drop dupe col
    combined_table['fname'] = combined_table.apply(lambda row: row['recruiter'] if pd.isna(row['fname']) else row['fname'], axis=1)
    combined_table['recruiter'] = combined_table.apply(lambda row: row['fname'] if pd.isna(row['recruiter']) else row['recruiter'], axis=1)
    combined_table.drop(columns=['fname'], inplace=True)

    #fill in missing alloc data
    #hours
    combined_table['hours_allocated'].fillna(0, inplace=True)
    #client/job - pulled from matching billing code
    def fill_client_job(row, ref_table):
        new_row = row.copy()
        if pd.isna(new_row['client']) or pd.isna(new_row['job']):
            match = ref_table.loc[ref_table['billing_code'] == new_row['billing_code']]
            if not match.empty:
                new_row['client'] = match['client'].iloc[0] if pd.isna(new_row['client']) else new_row['client']
                new_row['job'] = match['job'].iloc[0] if pd.isna(new_row['job']) else new_row['job']
        return new_row
    combined_table = combined_table.apply(fill_client_job, axis=1, args=(allocation,))

    #fill in blank hours with zero
    combined_table['hours_actual'].fillna(0, inplace=True)
    combined_table['hours_allocated'].fillna(0, inplace=True)

    #deal with nonunique billing codes
    # find dupe rows
    duplicates = combined_table.duplicated(subset=['recruiter', 'billing_code', 'hours_actual', 'client'], keep=False)
    duplicate_billing_codes = combined_table.loc[duplicates, 'billing_code'].unique()
    # Combine dupe rows, summing hours_allocated and keeping the first job
    combined_table = combined_table.groupby(['recruiter', 'billing_code', 'hours_actual', 'client'], as_index=False).agg({'hours_allocated': 'sum', 'job': 'first'})
    #fix job names
    def evaluate_job(row, duplicate_billing_codes):
        if row['billing_code'] in duplicate_billing_codes:
            if '> ' in row['billing_code']:
                return row['billing_code'].split('> ')[1]
            else:
                return 'n/a'
        return row['job']
    combined_table['job'] = combined_table.apply(evaluate_job, axis=1, args=(duplicate_billing_codes,))
    combined_table.sort_values(by=['client', 'job', 'recruiter'], inplace=True, ignore_index=True)
    combined_table['hours_actual'] = combined_table['hours_actual'].round(2)
    combined_table['hours_allocated'] = combined_table['hours_allocated'].round(2)

    print('Data load complete.')
    return combined_table, update_date

combined_table, update_date = load_data(client)

#color palette
recruiters = combined_table['recruiter'].unique()
colors = ['#e6194B', '#469990', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#800000',  '#808000', '#000075', '#fabed4', '#aaffc3', '#dcbeff']
recruiter_colors = {recruiter: colors[i % len(colors)] for i, recruiter in enumerate(recruiters)}


#define app
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
server = app.server


#generate progress bar components (label, bar, button, table, overbilled_alert) for arbitrary level of detail
def generate_bar_components(df, level):
    #print (f'level: {level}')
    nested_bars = []
    
    for item in df[level].unique():
        #filter data by level
        #print(f'filter: df[df[{level}] == {item}]')
        data = df[df[level] == item].copy()
        data = data.groupby([level, 'recruiter']).agg({'hours_actual': 'sum', 'hours_allocated': 'sum'}).reset_index()
        data['hours_actual'] = data['hours_actual'].round(2)
        data['hours_allocated'] = data['hours_allocated'].round(2)

        #define bulk properties
        alloc_total = data['hours_allocated'].sum()
        actual_total = data['hours_actual'].sum()
        if ['hours_allocated'] == 0 and ['hours_actual'] == 0: #stop if no hours data
            continue
        overbilled = actual_total > alloc_total
        id = data[level].iloc[0].replace(' ', '_').lower() + f"_{level}"
        pretty_id = data[level].iloc[0]
        #print(f'id:{id}, actual:{actual_total}, alloc:{alloc_total}, overbilled:{overbilled}')

        #generate nested bars for defined level of detail
        for _, row in data.iterrows():
            if row['hours_allocated'] == 0 and row['hours_actual'] == 0: #do not generate empty bars
                continue
            
            #generate row data
            row_id = row['recruiter'].replace(' ', '_').lower() + "_" + id
            row_user = row['recruiter']
            row_hours_actual = row['hours_actual']
            row_hours_allocated = row['hours_allocated']
            row_color = recruiter_colors.get(row_user, "gray")  # Default to "gray" if recruiter not in the dictionary
            #print (f"row:{row_id}, user:{row_user}, color:{row_color}, actual:{row_hours_actual}, alloc:{row_hours_allocated}")

            #append row data
            nested_bars.append(
                dbc.Progress(
                    value=row_hours_actual,
                    label=row_user,
                    bar=True,
                    color=row_color,
                    id=row_id + "_bar"
                )
            )

        # create components to display
        label = html.Div(pretty_id, id=id + "_label")
        bar = dbc.Progress(
            nested_bars,
            id=id + "_bar",
            max=alloc_total,
            style={'height': 30}
        )
        button = html.Button(
            "Show/Hide Data",
            id={'type':'button', 'id':id},
            n_clicks=0,
            style={'background-color':'#d3d3d3'}
        )
        table = dbc.Table.from_dataframe(
            data[['recruiter', 'hours_actual', 'hours_allocated']],
            size='sm',
            id={'type': 'table', 'id': id},
            style={'display': 'none'}
        )
        overbilled_alert = html.Div(
            "Over" if overbilled else None,
            id = id + "_overbilled",
            style={'color': 'red', 'font-weight': 'bold', 'font-size':'18px'}
        )
        
        return label, bar, button, table, overbilled_alert

#functions to collect bar components into containers, with different formatting for top-level
def generate_container(label, bar, button, table, alert):
    container = dbc.Container([
        dbc.Row([
            dbc.Col([label], style={'margin-bottom':10}),
            dbc.Col([alert], style={'margin-bottom':10,'text-align':'right', 'width':'10%'})
        ]),
        dbc.Row([bar], style={'margin-bottom':10}),
        dbc.Row([
            dbc.Col([button], style={'margin-bottom':10, 'width':'20%'})
        ]),
        dbc.Row([table], style={'margin-bottom':10})
        ], style={'margin-bottom':20})
    return container

def generate_top_level_container(label, bar, button, table, alert):
    container = dbc.Container([
        dbc.Row([
            dbc.Col([alert], style={'margin-bottom':10,'text-align':'right', 'width':'10%'})
        ]),
        dbc.Row([bar], style={'margin-bottom':10}),
        dbc.Row([
            dbc.Col([button], style={'margin-bottom':10, 'width':'20%'})
        ]),
        dbc.Row([table], style={'margin-bottom':10})
        ], style={'margin-bottom':0})
    return container


#generate all visuals for selected levels of detail
def generate_multilevel(df, levels):
    containers = []
    for level_0_value in df[levels[0]].unique():
        #print(f'level_0_value: {level_0_value}')
        level_0_df = df[df[levels[0]]== level_0_value]
        sub_containers = []

        #generate and collect level 1 bars
        for level_1_value in level_0_df[levels[1]].unique():
            #print(f'level_1_value: {level_1_value}')
            level_1_df = level_0_df[level_0_df[levels[1]] == level_1_value]
            label_1, bar_1, button_1, table_1, alert_1 = generate_bar_components(level_1_df, levels[1]) #generate bars for level 1
            sub_container_1 = generate_container(label_1, bar_1, button_1, table_1, alert_1)    #generate container for level 1 bars
            sub_containers.append(sub_container_1)  #collect level 1 containers

        #generate overall bars
        top_level_label = html.Div(level_0_value, style={"font-size": "20px", "font-weight": "bold", "margin-bottom": "10px"})
        #print(f'{top_level_label}, container_length: {len(sub_containers)}')
        top_level_container = dbc.Container([
            top_level_label,
            generate_top_level_container(*generate_bar_components(level_0_df, levels[0])),    #generate bars for level 0 and collect in top-level container
            dbc.Container([*sub_containers], style={'padding': 50}) if len(sub_containers) > 1 else None  #append level 1 containers only if multiple level 1 values
        ], style={'margin-bottom': 30, 'border': '1px solid #ccc', 'padding': '20px'})

        # Append the top-level container to the main containers list
        containers.append(top_level_container)
    return containers

containers = generate_multilevel(combined_table, ['client','billing_code'])



#layout
app.layout = dbc.Container([
    html.Div("Time Allocation Snapshot", style={"font-size": "24px", "font-weight": "bold", 'text-align':'center', 'margin-top':20}),
    dbc.Row([
        html.Div("Weekly Progress Bars", style={"font-size": "20px", "font-weight": "bold", 'margin-bottom':10}),
        dbc.Stack(containers, gap=2, id='containers')
    ]),
    html.Div(f"Last updated: {update_date}", id="update_date_label"),
    dbc.Row([dbc.Button("", id="refresh_button", n_clicks=0, style={'margin-top':0, 'opacity':0})], justify='left')
])

#toggle table buttons
@app.callback(
    Output({'type':'table', 'id':MATCH}, 'style'), 
    Input({'type':'button', 'id':MATCH}, 'n_clicks'),
    State({'type':'table', 'id':MATCH}, 'style') 
)
def toggle_table(n_clicks, current_style):
    if n_clicks > 0:
        if n_clicks %2 ==1:
            return {'display': 'block'}
        else:
            return {'display': 'none'}
    return current_style

#refresh data
@app.callback(
    Output('update_date_label', 'children'),
    Output('containers', 'children'),
    Input('refresh_button', 'n_clicks')
)
def refresh_data(n_clicks):
    if n_clicks > 0:
        print(f'refresh_data called, n_clicks: {n_clicks}')
        #refresh data
        global combined_table, update_date
        combined_table, update_date = load_data(client)
        update_date_label = f"Last updated: {update_date}"
        #refresh bars
        containers = generate_multilevel(combined_table, ['client','billing_code'])
        return update_date_label, containers
    return dash.no_update

#run app
if __name__ == "__main__":
    app.run()