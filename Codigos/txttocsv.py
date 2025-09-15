import pandas as pd

# Read the .txt file (assuming comma or tab separation)
df = pd.read_csv('ficheiros\\Voos1.txt', sep='\t|,', engine='python')  # Adjust `sep` if needed

# Save as .csv
df.to_csv('Voos.csv', index=False)