import aws_cdk as cdk
from stack import BlueGreenLambdaLinearStack

app = cdk.App()
BlueGreenLambdaLinearStack(app, "BlueGreenLambdaLinearStack",
                      env=cdk.Environment(region="us-east-1")       
                    )
app.synth()
